"""LLM call trace + cancel async-context primitives.

Extracted from llm_query_guards.py as a single business responsibility:
typed SDK tool-use trace capture for the operator SDK probe, plus structured
parent-driven cancellation context for the streaming LLM dispatch path.

All public symbols are re-exported by llm_query_guards.py for backward
compatibility.
"""

import contextlib
import contextvars


_LLM_CANCEL_CONTEXT = contextvars.ContextVar("llm_cancel_context", default=None)
_LLM_TOOL_TRACE = contextvars.ContextVar("llm_tool_trace", default=None)


@contextlib.contextmanager
def capture_llm_tool_trace():
    """Capture typed SDK tool-use/result events for the current async context.

    Normal role callers pay no tracing cost beyond one context lookup.  The
    operator SDK probe uses this scope to prove that the production streaming
    path really executed its required tools; parsing the human role log is not
    an execution receipt.
    """

    events = []
    token = _LLM_TOOL_TRACE.set(events)
    try:
        yield events
    finally:
        _LLM_TOOL_TRACE.reset(token)


def _record_llm_tool_trace_event(event):
    trace = _LLM_TOOL_TRACE.get()
    if not isinstance(trace, list):
        return
    payload = dict(event or {})
    payload["sequence"] = len(trace) + 1
    trace.append(payload)


@contextlib.contextmanager
def llm_cancel_scope(scope, reason="parent_timeout", timeout_sec=None):
    """Attach structured context to intentional parent-driven LLM cancellation."""
    payload = {
        "cancel_scope": str(scope),
        "cancel_reason": str(reason),
    }
    if timeout_sec is not None:
        try:
            payload["timeout_sec"] = float(timeout_sec)
        except (TypeError, ValueError):
            payload["timeout_sec"] = timeout_sec
    token = _LLM_CANCEL_CONTEXT.set(payload)
    try:
        yield
    finally:
        _LLM_CANCEL_CONTEXT.reset(token)


def _current_llm_cancel_context():
    context = _LLM_CANCEL_CONTEXT.get()
    return dict(context) if isinstance(context, dict) else {}


def _cancelled_event(base_category, parent_category, default_severity="warn"):
    context = _current_llm_cancel_context()
    if context.get("cancel_reason") == "parent_timeout":
        return parent_category, "info", context
    return base_category, default_severity, context
