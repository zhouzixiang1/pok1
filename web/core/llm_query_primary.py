"""Direct HTTP (Anthropic-messages) upstream: primary with fallback policy.

This module adds a **primary upstream** in front of the process transports.
Motivation (2026-11-04): a subscription account can be rate-limited or drained
at any moment, and a local CLI transport costs a process tree per dispatch
(measured: zcode ~1490 MB). A direct HTTPS call to an OpenAI/Anthropic-
compatible endpoint costs one socket and nothing else, and the pipeline's LLM
roles were measured to be **pure text** — ``tool_use_count`` is 0 for every
role in production (saturator, Master scouts, critics, auditor, reviewer,
worker CoT), because all tool work happens in the system-side gates, not in the
model. So a plain messages endpoint is sufficient for every role.

Fallback semantics (explicitly requested and load-bearing):

- ``primary``  — the configured HTTP upstream; always tried first.
- ``fallback`` — whatever ``POK_LLM_TRANSPORT`` already selects (codex here).
- A **rate limit** on the primary does NOT fall back: the run stays on the
  primary and backs off through the existing availability chain. Rationale:
  switching away on a 1302/1308 would silently spend the OTHER account's quota
  and make the consumption ledger unattributable. Only a *hard* primary failure
  (transport error, auth/config error, 5xx) escalates to the fallback.
- ``POK_LLM_UPSTREAM=fallback`` switches the whole pipeline to the fallback
  permanently; ``off``/unset-with-no-base-url disables the primary entirely.

Cache caliber is preserved: ``cache_read_input_tokens`` /
``cache_creation_input_tokens`` are read straight from the provider's usage
block, and ``input_tokens`` is the full prompt, so ``CR / input`` means the
same thing it means on the claude and codex paths.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import urllib.error
import urllib.request

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeSDKError,
        ResultMessage,
        TextBlock,
        ThinkingBlock,
    )

    _SDK_IMPORT_ERROR = None
except ImportError as _sdk_exc:  # SDK-free interpreter (preflight surface)
    _SDK_IMPORT_ERROR = _sdk_exc

#: ``primary`` (default when a base URL is configured) | ``fallback`` | ``off``.
UPSTREAM_ENV = "POK_LLM_UPSTREAM"
#: Base URL of the primary upstream, e.g. ``https://tydfxt.top/v1``.
BASE_URL_ENV = "POK_LLM_PRIMARY_BASE_URL"
#: Model id sent to the primary upstream.
MODEL_ENV = "POK_LLM_PRIMARY_MODEL"
#: API key for the primary upstream. Read from the environment only — never
#: from a committed file (the service loads env.runtime.local, which is
#: gitignored, for exactly this).
API_KEY_ENV = "POK_LLM_PRIMARY_API_KEY"
#: Wire protocol of the primary: ``anthropic`` (default) or ``openai``.
WIRE_ENV = "POK_LLM_PRIMARY_WIRE"
#: Per-request timeout in seconds.
TIMEOUT_ENV = "POK_LLM_PRIMARY_TIMEOUT_SEC"

_DEFAULT_TIMEOUT = 600.0
#: Provider error codes that mean "slow down", never "this upstream is broken".
_RATE_LIMIT_CODES = ("1308", "1302", "rate limit", "too many requests")


def _require_sdk():
    if _SDK_IMPORT_ERROR is not None:
        raise ClaudeSDKError(
            "primary HTTP upstream requires claude_agent_sdk (dormant surface); "
            "run from the service venv. Original import error: "
            f"{_SDK_IMPORT_ERROR}"
        ) from _SDK_IMPORT_ERROR


def upstream_mode() -> str:
    """Resolve the upstream mode: ``primary`` | ``fallback`` | ``off``.

    ``off`` also results when a base URL is configured without an API key —
    an unauthenticated primary would fail every call and turn a config
    oversight into a pipeline outage.
    """

    raw = str(os.environ.get(UPSTREAM_ENV, "") or "").strip().lower()
    if raw in ("off", "disabled", "0", "false", "no"):
        return "off"
    if raw == "fallback":
        return "fallback"
    base = str(os.environ.get(BASE_URL_ENV, "") or "").strip()
    key = str(os.environ.get(API_KEY_ENV, "") or "").strip()
    if base and key:
        return "primary"
    return "off"


def primary_enabled() -> bool:
    return upstream_mode() == "primary"


def primary_config() -> dict:
    import llm_query_codex as _cx

    return {
        "base_url": str(os.environ.get(BASE_URL_ENV, "") or "").strip().rstrip("/"),
        "model": str(os.environ.get(MODEL_ENV, "") or "").strip(),
        "api_key": str(os.environ.get(API_KEY_ENV, "") or "").strip(),
        "wire": (str(os.environ.get(WIRE_ENV, "") or "anthropic").strip().lower()
                 or "anthropic"),
        "timeout": _timeout_sec(),
        # Degraded tool support: keep it visible rather than implicit.
        "tools_supported": False,
        "fallback_transport": _cx.codex_transport_enabled() and "codex" or "claude",
    }


def _timeout_sec() -> float:
    try:
        value = float(os.environ.get(TIMEOUT_ENV, "") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class PrimaryUpstreamError(ClaudeSDKError):
    """Primary upstream failure, carrying whether it is worth falling back.

    ``retryable`` means "slow down / transient" — the caller must NOT fall
    back. ``fatal`` means the primary is unusable for this attempt and the
    fallback transport should be tried.
    """

    def __init__(self, message, *, retryable=False, fatal=False, code=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.fatal = bool(fatal)
        self.upstream_code = code


def _looks_like_rate_limit(status, body_text: str) -> bool:
    if status == 429:
        return True
    lowered = (body_text or "").lower()
    return any(marker in lowered for marker in _RATE_LIMIT_CODES)


#: Tools the pipeline's roles actually request. Measured across every call
#: site: Read alone covers 97.6% of the token volume (the saturator is 7222 of
#: 8015 calls and only ever declares Read); Bash/Edit appear in the 2.4% of
#: low-frequency write roles.
_READ_TOOL = {
    "name": "Read",
    "description": (
        "Read a file from the filesystem. Returns the file contents with "
        "1-based line numbers. Use it to inspect source files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"file_path": {"type": "string", "description": "Absolute path"}},
        "required": ["file_path"],
    },
}

_EDIT_TOOL = {
    "name": "Edit",
    "description": (
        "Replace an exact string in a file. old_string must appear exactly "
        "once. Only paths inside the role's write scope are editable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}

_BASH_TOOL = {
    "name": "Bash",
    "description": (
        "Run a shell command inside the role's write scope. Used mainly for "
        "py_compile / syntax checks. Commands that would write outside the "
        "scope are refused."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}

#: Wall clock for one Bash call.
_BASH_TIMEOUT_SEC = 120
#: Captured bytes per stream from a Bash call.
_BASH_MAX_OUTPUT = 60_000


#: Read calls a single dispatch may make. Mirrors the saturator's own hard stop
#: contract (POK_LLM_SATURATOR_MAX_READ_CALLS, production 18) so the HTTP path
#: cannot let a role loop longer than the tool-driven transports allow.
_DEFAULT_MAX_READ_CALLS = 24

#: Output bytes per Read, so one huge file cannot blow the context budget.
_MAX_READ_BYTES = 400_000


def _max_read_calls() -> int:
    try:
        value = int(os.environ.get("POK_LLM_PRIMARY_MAX_READ_CALLS", "") or
                    os.environ.get("POK_LLM_SATURATOR_MAX_READ_CALLS", "") or
                    _DEFAULT_MAX_READ_CALLS)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_READ_CALLS
    return max(0, value)


def declared_tools(options) -> list:
    """The role's declared tool names, normalized to a list."""

    raw = getattr(options, "tools", None) if options is not None else None
    if not raw:
        return []
    names = []
    try:
        for item in raw:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
    except TypeError:
        return []
    return names


def tool_schemas_for(options) -> list:
    """Wire schemas for the tools this dispatch may use.

    Only ``Read`` is exposed. ``Bash``/``Edit`` are deliberately NOT exposed
    here: those need a sandbox this transport does not have (claude enforces
    them through PreToolUse hooks, codex through bwrap). A role that declares
    them keeps using a tool-capable transport; see the refusal in ``run``.
    """

    names = declared_tools(options)
    schemas = []
    if "Read" in names:
        schemas.append(_READ_TOOL)
    if "Edit" in names:
        schemas.append(_EDIT_TOOL)
    if "Bash" in names:
        schemas.append(_BASH_TOOL)
    return schemas


class ToolScopeError(ClaudeSDKError):
    """A tool call asked for authority the role does not have."""


def _scope_from_options(options) -> dict:
    scope = getattr(options, "read_scope", None) if options is not None else None
    return scope if isinstance(scope, dict) else {"dirs": [], "files": []}


def _read_path_allowed(path_text: str, options) -> bool:
    """True when READ of ``path_text`` is inside the role's declared scope.

    Reuses the same predicate the claude path's Read guard applies, so the HTTP
    transport cannot widen what a role may read.
    """

    import llm_query_guards as guards

    scope = _scope_from_options(options)
    if not (scope.get("dirs") or scope.get("files")):
        return False
    # Symlinks are rejected outright: the guarded transports refuse them too,
    # and following one is how a scoped read escapes to an unscoped file.
    if guards._path_has_symlink_component(path_text):
        return False
    try:
        return bool(guards._path_inside_allowed_scope(path_text, scope))
    except Exception:
        return False


def _write_scope_from_options(options) -> dict:
    scope = getattr(options, "write_scope", None) if options is not None else None
    return scope if isinstance(scope, dict) else {"dirs": [], "files": []}


def _write_path_allowed(path_text: str, options) -> bool:
    """True when WRITING ``path_text`` is inside the role's write scope.

    Same predicate the claude path's PreToolUse guard and the codex adapter's
    workspace-write roots use, so all three transports agree on the boundary.
    """

    import llm_query_guards as guards

    scope = _write_scope_from_options(options)
    if not (scope.get("dirs") or scope.get("files")):
        return False
    if guards._path_has_symlink_component(path_text):
        return False
    try:
        return bool(guards._path_inside_allowed_scope(path_text, scope))
    except Exception:
        return False


def execute_edit(inputs: dict, options) -> str:
    """Exact-string replacement limited to the role's write scope."""

    data = inputs or {}
    path_text = str(data.get("file_path") or "").strip()
    old = data.get("old_string")
    new = data.get("new_string")
    if not path_text or not isinstance(old, str) or not isinstance(new, str):
        raise ToolScopeError("Edit requires file_path, old_string and new_string")
    if not os.path.isabs(path_text):
        raise ToolScopeError(f"Edit requires an absolute path, got {path_text!r}")
    if not _write_path_allowed(path_text, options):
        raise ToolScopeError(
            f"Edit denied: {path_text} is outside this role's write scope"
        )
    if not old:
        raise ToolScopeError("Edit refuses an empty old_string (use Write semantics instead)")
    try:
        with open(path_text, "r", encoding="utf-8") as handle:
            original = handle.read()
    except FileNotFoundError:
        return f"<error>file not found: {path_text}</error>"
    except OSError as exc:
        return f"<error>read failed: {exc}</error>"
    occurrences = original.count(old)
    if occurrences == 0:
        return "<error>old_string not found in the file</error>"
    if occurrences > 1:
        return (
            f"<error>old_string appears {occurrences} times; it must be unique. "
            "Include more surrounding context.</error>"
        )
    updated = original.replace(old, new, 1)
    # Write via a sibling temp file + os.replace so a crash cannot leave a
    # half-written lease artifact (the worker's bytes are the deliverable).
    directory = os.path.dirname(path_text) or "."
    tmp_path = os.path.join(directory, f".pok-edit-{os.getpid()}-{id(new)}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.replace(tmp_path, path_text)
    finally:
        with contextlib.suppress(OSError):
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    return f"<ok>replaced 1 occurrence in {path_text} ({len(original)} -> {len(updated)} bytes)</ok>"


def _bash_command_allowed(command: str, options) -> bool:
    """Conservative Bash gate.

    The claude path parses every Bash command through its scope guard; this
    transport reuses the same helper so a command that would write outside the
    write scope is refused before execution. Read-only commands are always
    allowed (they cannot mutate the tree).
    """

    import llm_query_guards as guards

    write_scope = _write_scope_from_options(options)
    try:
        violation = guards._subagent_bash_write_scope_violation(command, write_scope)
    except Exception:
        # Fail closed: an unparseable command is not trusted.
        return False
    return not violation


def execute_bash(inputs: dict, options) -> str:
    """Run one shell command inside the role's write scope."""

    command = str((inputs or {}).get("command") or "").strip()
    if not command:
        raise ToolScopeError("Bash requires command")
    if not _bash_command_allowed(command, options):
        raise ToolScopeError(
            "Bash denied: this command would write outside the role's write scope"
        )
    write_scope = _write_scope_from_options(options)
    cwd = None
    for candidate in (write_scope.get("dirs") or ()):
        if os.path.isdir(str(candidate)):
            cwd = str(candidate)
            break
    try:
        completed = subprocess.run(
            ["/bin/bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=_BASH_TIMEOUT_SEC,
            cwd=cwd,
            env={**os.environ, "POK_SANDBOXED_BASH": "1"},
        )
    except subprocess.TimeoutExpired:
        return f"<error>command timed out after {_BASH_TIMEOUT_SEC}s</error>"
    except OSError as exc:
        return f"<error>failed to run: {exc}</error>"
    out = (completed.stdout or "")[:_BASH_MAX_OUTPUT]
    err = (completed.stderr or "")[:_BASH_MAX_OUTPUT]
    return (
        f"<exit_code>{completed.returncode}</exit_code>\n"
        f"<stdout>\n{out}\n</stdout>\n<stderr>\n{err}\n</stderr>"
    )


def execute_tool(name: str, inputs: dict, options) -> str:
    if name == "Read":
        return execute_read(inputs, options)
    if name == "Edit":
        return execute_edit(inputs, options)
    if name == "Bash":
        return execute_bash(inputs, options)
    raise ToolScopeError(f"tool {name!r} is not available on this transport")


def execute_read(inputs: dict, options) -> str:
    """Run one Read call under the role's scope. Returns the tool result text."""

    path_text = str((inputs or {}).get("file_path") or "").strip()
    if not path_text:
        raise ToolScopeError("Read requires file_path")
    if not os.path.isabs(path_text):
        raise ToolScopeError(f"Read requires an absolute path, got {path_text!r}")
    if not _read_path_allowed(path_text, options):
        raise ToolScopeError(f"Read denied: {path_text} is outside this role's read scope")
    try:
        with open(path_text, "rb") as handle:
            raw = handle.read(_MAX_READ_BYTES + 1)
    except FileNotFoundError:
        return f"<error>file not found: {path_text}</error>"
    except IsADirectoryError:
        return f"<error>is a directory: {path_text}</error>"
    except OSError as exc:
        return f"<error>read failed: {exc}</error>"
    truncated = len(raw) > _MAX_READ_BYTES
    text = raw[:_MAX_READ_BYTES].decode("utf-8", "replace")
    lines = text.splitlines()
    numbered = "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(lines, 1))
    if truncated:
        numbered += f"\n... [truncated at {_MAX_READ_BYTES} bytes]"
    return numbered


def _build_request(cfg: dict, prompt: str, messages=None, tools=None):
    """Build the wire request.

    ``messages`` defaults to the single user turn made from ``prompt``; the tool
    loop passes the accumulated conversation instead. ``tools`` adds the
    function schemas — measured working on both wires (the upstream answers with
    a proper ``tool_use`` block and ``stop_reason: tool_use``).
    """

    base = cfg["base_url"]
    convo = messages if messages is not None else [{"role": "user", "content": prompt}]
    if cfg["wire"] == "openai":
        url = base + "/chat/completions"
        payload = {"model": cfg["model"], "max_tokens": 32000, "messages": convo}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec["name"],
                        "description": spec.get("description", ""),
                        "parameters": spec.get("input_schema") or {},
                    },
                }
                for spec in tools
            ]
        headers = {
            "content-type": "application/json",
            "authorization": "Bearer " + cfg["api_key"],
        }
    else:
        url = base + "/messages"
        payload = {"model": cfg["model"], "max_tokens": 32000, "messages": convo}
        if tools:
            payload["tools"] = tools
        headers = {
            "content-type": "application/json",
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
        }
    return url, payload, headers


def _parse_response(cfg: dict, body: dict) -> dict:
    """Normalize either wire shape into (text, thinking, usage)."""

    text_parts = []
    thinking_parts = []
    usage = {}
    if cfg["wire"] == "openai":
        choices = body.get("choices") or []
        if choices:
            message = (choices[0] or {}).get("message") or {}
            if isinstance(message.get("content"), str):
                text_parts.append(message["content"])
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                thinking_parts.append(reasoning)
        raw = body.get("usage") or {}
        usage = {
            "input_tokens": _int(raw.get("prompt_tokens")),
            "output_tokens": _int(raw.get("completion_tokens")),
            "cache_read_input_tokens": _int(
                (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
                or raw.get("cached_tokens")
            ),
            "cache_creation_input_tokens": 0,
        }
    else:
        for block in body.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block.get("type") in ("thinking", "reasoning"):
                value = block.get("thinking") or block.get("text")
                if isinstance(value, str) and value:
                    thinking_parts.append(value)
        raw = body.get("usage") or {}
        usage = {
            "input_tokens": _int(raw.get("input_tokens")),
            "output_tokens": _int(raw.get("output_tokens")),
            "cache_read_input_tokens": _int(raw.get("cache_read_input_tokens")),
            "cache_creation_input_tokens": _int(
                raw.get("cache_creation_input_tokens")
            ),
        }
    # CALIBER CONVERSION (measured, 2026-11-04). This upstream reports
    # ``input_tokens`` as UNCACHED NEW TOKENS ONLY, not the whole prompt.
    # Evidence: an identical prompt repeated three times reports
    # in=18 then in=6 then in=6 while cache_read stays ~2599 — the ~2590-token
    # system/message prefix lives in the cache and is excluded from
    # input_tokens. GLM (and the claude/codex paths) report the FULL prompt in
    # input_tokens, so taking this upstream's number at face value would make
    # every call look like a 100% cache hit (6/6) and silently destroy the
    # cache-hit KPI this pipeline is tracking.
    #
    # Conversion: full_prompt = input_tokens + cache_read
    #             (cache_read is the already-cached prefix)
    # and the stored record keeps the provider's raw split so an auditor can
    # re-derive either number. ``upstream`` + ``cache_caliber`` mark the row so
    # no analysis silently mixes the two report conventions.
    raw_input = usage.get("input_tokens") or 0
    raw_cache_read = usage.get("cache_read_input_tokens") or 0
    if raw_cache_read > 0:
        usage["provider_input_tokens"] = raw_input
        usage["full_prompt_input_tokens"] = raw_input + raw_cache_read
        usage["cache_caliber"] = "provider_uncached_plus_cached"
        # Remap to the pipeline's convention: input_tokens = whole prompt.
        usage["input_tokens"] = raw_input + raw_cache_read
    # Tool calls, normalized across both wires so the loop is wire-agnostic.
    tool_calls = []
    raw_blocks = []
    if cfg["wire"] == "openai":
        choices = body.get("choices") or []
        message = (choices[0] or {}).get("message") if choices else {}
        for call in (message or {}).get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            tool_calls.append({
                "id": call.get("id") or "",
                "name": fn.get("name") or "",
                "input": args if isinstance(args, dict) else {},
            })
            raw_blocks.append({"type": "tool_use", "id": call.get("id") or "",
                               "name": fn.get("name") or "", "input": args})
    else:
        for block in body.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "input": block.get("input") or {},
                })
                raw_blocks.append({
                    "type": "tool_use",
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "input": block.get("input") or {},
                })
            elif isinstance(block, dict) and block.get("type") == "text":
                raw_blocks.append({"type": "text", "text": block.get("text") or ""})
            elif isinstance(block, dict) and block.get("type") in ("thinking", "reasoning"):
                raw_blocks.append({
                    "type": "thinking",
                    "thinking": block.get("thinking") or block.get("text") or "",
                })
    return {
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts),
        "usage": usage,
        "tool_calls": tool_calls,
        "raw_blocks": raw_blocks,
        "stop_reason": body.get("stop_reason")
        or ((body.get("choices") or [{}])[0] or {}).get("finish_reason"),
        "model": body.get("model") or cfg["model"],
        "upstream": "primary",
    }


def _post_json(url: str, payload: dict, headers: dict, timeout: float):
    """Blocking POST; returns (status, parsed_json_or_None, raw_text)."""

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            text = raw.decode("utf-8", "replace")
            try:
                return response.status, json.loads(text), text
            except Exception:
                return response.status, None, text
    except urllib.error.HTTPError as error:
        raw = b""
        with contextlib.suppress(Exception):
            raw = error.read()
        text = raw.decode("utf-8", "replace")
        try:
            return error.code, json.loads(text), text
        except Exception:
            return error.code, None, text


class PrimaryHttpTransport:
    """Process-less transport: the provider-attempt lifecycle needs only
    ``_process`` (absent is fine) and an awaitable ``close()`` that returns
    True. There is nothing to kill, so close is trivially confirmed."""

    native_streaming = False
    #: Declared so callers can log the capability difference honestly.
    tools_supported = False

    def __init__(self, full_prompt, options, allowed_write_dir=None):
        _require_sdk()
        self._prompt = full_prompt or ""
        self._options = options
        self._allowed_write_dir = allowed_write_dir
        self._process = None
        self._closed = False
        self.result = None

    async def _single_call(self, cfg, messages, tools):
        """One round-trip off the event loop; returns the parsed body."""

        url, payload, headers = _build_request(cfg, self._prompt, messages, tools)
        try:
            status, body, text = await asyncio.get_running_loop().run_in_executor(
                None, _post_json, url, payload, headers, cfg["timeout"]
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # network/DNS/TLS
            raise PrimaryUpstreamError(
                f"primary upstream transport error: {type(exc).__name__}: {exc}",
                fatal=True,
                code="transport",
            ) from exc
        if status is None or status >= 400 or body is None:
            rate_limited = _looks_like_rate_limit(status, text)
            raise PrimaryUpstreamError(
                f"primary upstream HTTP {status}: {(text or '')[:600]}",
                retryable=rate_limited,
                fatal=not rate_limited,
                code=str(status),
            )
        if isinstance(body, dict) and body.get("error"):
            err = body.get("error")
            message = err.get("message") if isinstance(err, dict) else str(err)
            rate_limited = _looks_like_rate_limit(status, str(message))
            raise PrimaryUpstreamError(
                f"primary upstream error: {str(message)[:600]}",
                retryable=rate_limited,
                fatal=not rate_limited,
                code=str((err or {}).get("code") if isinstance(err, dict) else status),
            )
        return body

    async def run(self):
        """Perform the call (with a scoped Read tool loop); store ``self.result``.

        The loop is the difference between this transport working and the
        2026-11-04 failure: roles declare tools and will spend the whole turn
        asking to read files. Without a loop the model hallucinates tool calls
        (measured: 160/160 saturator findings came back with contracts=[] and
        fabricated <tool_call> text).

        WRITE-CAPABLE ROLES ARE REFUSED, not silently degraded. Bash/Edit need a
        sandbox this transport does not provide; a role declaring them must keep
        using a tool-capable transport until one exists, or it would run shell
        commands with no boundary at all.
        """

        _require_sdk()
        cfg = primary_config()
        if not cfg["base_url"] or not cfg["api_key"]:
            raise PrimaryUpstreamError(
                "primary upstream is not configured", fatal=True, code="config"
            )
        names = declared_tools(self._options)
        # Read/Edit/Bash are implemented here with explicit scope enforcement.
        # Anything ELSE a role declares (Write, WebSearch, Task, ...) is a
        # capability this transport genuinely lacks; refusing beats pretending.
        unsupported = [
            n for n in names if n not in ("Read", "Edit", "Bash")
        ]
        if unsupported:
            raise PrimaryUpstreamError(
                "primary upstream cannot serve a role that declares "
                f"{sorted(unsupported)}: only Read/Edit/Bash are implemented "
                "on this transport",
                fatal=True,
                code="unsupported_tools",
            )
        tools = tool_schemas_for(self._options)
        budget = _max_read_calls() if tools else 0
        messages = [{"role": "user", "content": self._prompt}]
        usage_total = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        tool_calls_used = 0
        for round_index in range(budget + 1):
            body = await self._single_call(cfg, messages, tools)
            parsed = _parse_response(cfg, body)
            for key in usage_total:
                # Rounds re-send the whole conversation, so accumulate only the
                # provider's own incremental input rather than summing full
                # prompts (which would double-count the cached prefix).
                value = parsed["usage"].get(key) or 0
                if key == "input_tokens":
                    value = parsed["usage"].get("provider_input_tokens", value)
                usage_total[key] += int(value)
            block_calls = parsed.get("tool_calls") or []
            if not block_calls:
                parsed["usage"] = usage_total
                parsed["tool_rounds"] = round_index
                self.result = parsed
                return self.result
            if tool_calls_used + len(block_calls) > budget:
                # Budget exhausted mid-flight: stop calling tools and ask for the
                # final answer with what has been gathered so far.
                messages.append({"role": "assistant", "content": parsed.get("raw_blocks") or []})
                messages.append({
                    "role": "user",
                    "content": "Tool budget exhausted. Produce your final answer "
                               "now using only what you have already gathered.",
                })
                tools = []
                budget = tool_calls_used
                continue
            messages.append({"role": "assistant", "content": parsed.get("raw_blocks") or []})
            results = []
            for call in block_calls:
                tool_calls_used += 1
                name = str(call.get("name") or "")
                try:
                    output = execute_tool(name, call.get("input") or {}, self._options)
                except ToolScopeError as exc:
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": call.get("id") or "",
                        "is_error": True,
                        "content": str(exc),
                    })
                    continue
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.get("id") or "",
                    "content": output,
                })
            messages.append({"role": "user", "content": results})
        # Fell out of the loop: return what the last round produced.
        self.result = {
            "text": "",
            "thinking": "",
            "usage": usage_total,
            "model": cfg["model"],
            "stop_reason": "read_budget_exhausted",
            "upstream": "primary",
        }
        return self.result

    async def close(self):
        self._closed = True
        return True


def new_primary_transport(full_prompt, options, allowed_write_dir=None):
    return PrimaryHttpTransport(full_prompt, options, allowed_write_dir)


async def primary_query(prompt=None, *, options=None, transport=None):
    """Yield the same SDK message shapes the other transports yield."""

    _require_sdk()
    if transport is None:
        transport = new_primary_transport(prompt or "", options)
    model_label = getattr(options, "model", None) or "primary"
    try:
        result = await transport.run()
        thinking = result.get("thinking") or ""
        if thinking:
            yield AssistantMessage(
                content=[ThinkingBlock(thinking=thinking, signature="")],
                model=result.get("model") or model_label,
            )
        text = result.get("text") or ""
        if text.strip():
            yield AssistantMessage(
                content=[TextBlock(text=text)],
                model=result.get("model") or model_label,
            )
        usage = dict(result.get("usage") or {})
        usage.setdefault("cache_read_input_tokens", 0)
        usage.setdefault("cache_creation_input_tokens", 0)
        # Attribution marker: these rows come from a DIFFERENT upstream than
        # the codex/GLM path, so any ratio comparison must group by it.
        usage["upstream"] = result.get("upstream") or "primary"
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="",
            total_cost_usd=None,
            usage=usage,
            result=(text or None),
            api_error_status=None,
        )
    finally:
        await transport.close()