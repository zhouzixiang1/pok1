"""Event classification policy for restart/observe runs."""

ALERT_EVENTS = {
    "daemon.crashed",
    "daemon.exited_cleanly",
    "orchestrator.crashed",
    "orchestrator.recovery_blocked",
    "orchestrator.recovery_blocked_stop",
    "pipeline.quality_failed",
    "pipeline.guard_block",
    "pipeline.subagent_guard_block",
    "pipeline.subagent_readonly_guard_block",
    "pipeline.redundant_tool_call",
    "pipeline.sdk_stream_error",
    "pipeline.llm_role_cancelled",
    "pipeline.llm_role_stream_cancelled",
    "pipeline.llm_role_parent_timeout_cancelled",
    "pipeline.llm_role_stream_parent_timeout_cancelled",
    "pipeline.abandon_refused_state_guard",
    "pipeline.precommit_eval",
    "pipeline.precommit_infra_timeout",
    "pipeline.prepare_blocked_runtime_guard",
    "repo.runtime_guard_blocked",
}

FATAL_EVENTS = {
    "orchestrator.recovery_blocked",
    "orchestrator.recovery_blocked_stop",
    "pipeline.subagent_guard_block",
    "pipeline.subagent_readonly_guard_block",
    "pipeline.prepare_blocked_runtime_guard",
    "repo.runtime_guard_blocked",
}

PARENT_TIMEOUT_EVENTS = {
    "pipeline.llm_role_parent_timeout_cancelled",
    "pipeline.llm_role_stream_parent_timeout_cancelled",
}

LEGACY_EXPECTED_CANCELS = {
    ("DYNAMIC_TEST_GEN", "workers_done"),
    ("battle_experience", None),
}


def is_expected_event(event):
    """Return True for noisy-but-planned events that should not alert/fail."""
    event_type = event.get("type")
    data = event.get("data") or {}
    role = str(data.get("role") or "")
    stage = data.get("stage")
    if event_type in PARENT_TIMEOUT_EVENTS:
        return True
    if event_type in {"pipeline.llm_role_cancelled", "pipeline.llm_role_stream_cancelled"}:
        if (role, stage) in LEGACY_EXPECTED_CANCELS or (role, None) in LEGACY_EXPECTED_CANCELS:
            return True
        if role.startswith("LITERATURE_PROBE") and stage == "direction_audited":
            return True
    return False


def should_alert(event):
    event_type = event.get("type")
    if is_expected_event(event):
        return False
    if event_type == "pipeline.precommit_eval":
        return not bool((event.get("data") or {}).get("passed", True))
    return event_type in ALERT_EVENTS


def is_fatal_event(event):
    if is_expected_event(event):
        return False
    return event.get("type") in FATAL_EVENTS
