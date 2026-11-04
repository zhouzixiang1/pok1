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


def _build_request(cfg: dict, prompt: str):
    base = cfg["base_url"]
    if cfg["wire"] == "openai":
        url = base + "/chat/completions"
        payload = {
            "model": cfg["model"],
            "max_tokens": 32000,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "content-type": "application/json",
            "authorization": "Bearer " + cfg["api_key"],
        }
    else:
        url = base + "/messages"
        payload = {
            "model": cfg["model"],
            "max_tokens": 32000,
            "messages": [{"role": "user", "content": prompt}],
        }
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
    # Caliber guard. Measured on tydfxt.top: a tiny prompt reported
    # input_tokens=18 with cache_read_input_tokens=2587 — the proxy reports a
    # cache figure that is NOT bounded by this request's prompt, so raw
    # CR/input is meaningless (143.7) and would poison any cross-upstream
    # comparison. ``llm_call_metrics`` already clamps with
    # cache_eligible = max(input, cr+cw) so the stored rate stays <= 1, but the
    # RATIO ITSELF is not comparable to GLM's. We therefore (a) keep the raw
    # numbers for the record, (b) mark the row so no analysis mixes the two
    # calibers, and (c) never let cr exceed input in the mapped SDK usage
    # (which is what downstream ratio code reads).
    raw_cache_read = usage.get("cache_read_input_tokens") or 0
    input_tokens = usage.get("input_tokens") or 0
    if input_tokens and raw_cache_read > input_tokens:
        usage["provider_cache_read_input_tokens"] = raw_cache_read
        usage["cache_caliber"] = "provider_unbounded_prompt_scoped"
        usage["cache_read_input_tokens"] = input_tokens
    return {
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts),
        "usage": usage,
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

    async def run(self):
        """Perform the call; store ``self.result`` or raise with a verdict."""

        _require_sdk()
        cfg = primary_config()
        if not cfg["base_url"] or not cfg["api_key"]:
            raise PrimaryUpstreamError(
                "primary upstream is not configured", fatal=True, code="config"
            )
        url, payload, headers = _build_request(cfg, self._prompt)
        try:
            status, body, text = await asyncio.get_running_loop().run_in_executor(
                None, _post_json, url, payload, headers, cfg["timeout"]
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # network/DNS/TLS — the primary is unusable here
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
        self.result = _parse_response(cfg, body)
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