"""LLM infrastructure error classification.

Distinguishes LLM infrastructure failures (SDK crashes, timeouts, connection
errors) from real business judgements (parse errors, validation failures).

Infrastructure errors must NOT be disguised as strategic rejections — they
should trigger neutral retry/soft-abandon behaviour, not pipeline rollback or
worker code destruction.
"""

import asyncio

from claude_agent_sdk import ClaudeSDKError


def is_llm_infra_error(exc) -> bool:
    """True = LLM infrastructure error (retry/neutral/mark), not a real business failure.

    Type-based, not keyword-based: ClaudeSDKError is the strong signal (flat class
    covering SDK signature errors, 529 exhaustion, etc.). Keyword matching would
    misfire on real business exceptions containing words like "signature"/"timeout".
    """
    return isinstance(exc, (ClaudeSDKError, asyncio.TimeoutError, ConnectionError, OSError))


def infra_payload(exc, **fields) -> dict:
    """Infra return value carrying the `llm_failed` marker.

    `fields` preserves the original safe defaults for backward compatibility with
    upstream callers that read e.g. `approved`/`score`.
    """
    return {"llm_failed": True, "infra_error": True, "error": str(exc), **fields}
