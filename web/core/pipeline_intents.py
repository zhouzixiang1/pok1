"""Structured MCP tool intents.

The LLM-facing ``directive`` text is kept for compatibility, but tools should
also return this small machine-readable payload so callers do not need to parse
natural language to choose the next pipeline transition.
"""


def make_intent(kind: str, *, next_tool: str | None = None,
                failure_class: str | None = None,
                authority: str | None = None,
                safe_to_auto_execute: bool = False,
                reason: str | None = None) -> dict:
    data = {
        "kind": kind,
        "safe_to_auto_execute": bool(safe_to_auto_execute),
    }
    if next_tool:
        data["next_tool"] = next_tool
    if failure_class:
        data["failure_class"] = failure_class
    if authority:
        data["authority"] = authority
    if reason:
        data["reason"] = reason
    return data
