"""Shared failure classification for pipeline gates.

This module is intentionally dependency-light so core state helpers, MCP tools,
and observe code can agree on failure classes without importing each other.
"""

INFRA_BLOCKER_REASONS = {
    "match_timeout",
    "incomplete_or_timeout",
    "scheduler_error",
    "match_exception",
}


def is_infra_blocker(reason) -> bool:
    """Return True when a precommit blocker is infrastructure, not strategy."""
    return str(reason or "") in INFRA_BLOCKER_REASONS


def classify_precommit_gate(gate: dict | None) -> str:
    """Classify a persisted precommit gate result.

    Returns one of:
    - ``not_run``: no gate exists yet
    - ``passed``: precommit passed
    - ``infra_timeout``: all blockers are infra-only
    - ``regression``: at least one blocker is a bot regression
    - ``failed_unknown``: failed but blockers are absent/ambiguous
    """
    if not gate:
        return "not_run"
    if gate.get("passed") is True:
        return "passed"
    blockers = gate.get("blockers") or []
    if not blockers:
        return "failed_unknown"
    reasons = []
    for blocker in blockers:
        if isinstance(blocker, dict):
            reasons.append(blocker.get("reason"))
        else:
            reasons.append(blocker)
    if reasons and all(is_infra_blocker(reason) for reason in reasons):
        return "infra_timeout"
    return "regression"
