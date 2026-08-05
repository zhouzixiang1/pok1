"""Shared failure classification for pipeline gates.

This module is intentionally dependency-light so core state helpers, MCP tools,
and observe code can agree on failure classes without importing each other.
"""

INFRA_BLOCKER_REASONS = {
    "match_timeout",
    "incomplete_or_timeout",
    "scheduler_error",
    "match_exception",
    # Native TCP runner blocker vocabulary (national_native_acceptance.py and
    # tool_eval.py).  These are produced when a native precommit run is killed
    # mid-match (startup-watchdog kill, transport stall, launch-latency spike)
    # or raises an exception — all harness/infrastructure failures independent
    # of the candidate's policy bytes (the bot runtime is system-owned and
    # byte-identical across every candidate and baseline opponent).  Without
    # these in the infra set, ``classify_precommit_gate`` returns ``regression``
    # for a pure transient stall, which the Slice-2b consumer then rejects as a
    # permanent candidate_failure with zero retries — the same class of bug as
    # the smoke-gate single-shot abandon.
    "native_incomplete_match",
    "native_precommit_sample_shortfall",
    "native_no_samples",
    "native_precommit_exception",
    "national_precommit_exception",
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
