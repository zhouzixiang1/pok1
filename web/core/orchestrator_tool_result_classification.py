"""Pipeline tool-result classifier set.

Extracted from orchestrator.py as a single business responsibility: decode
SDK tool results and classify them into typed recovery capabilities
(worker circuit-breaker / terminal-abandon / operator-shutdown-interrupted /
precommit-rework circuit-breaker / official-rework circuit-breaker /
crossover incompatible / crossover LLM-exhausted / master-ensemble
pending-retry).

All symbols are re-exported by orchestrator.py for backward compatibility.
"""

import json
import math


def _extract_tool_result_json(result):
    try:
        content = result.get("content") if isinstance(result, dict) else None
        if not content:
            return {}
        first = content[0] if isinstance(content, list) else content
        text = first.get("text") if isinstance(first, dict) else None
        if not text:
            return {}
        return json.loads(text)
    except Exception:
        return {}


def _is_worker_circuit_breaker_result(data):
    if not isinstance(data, dict):
        return False
    error = str(data.get("error") or "")
    return "CIRCUIT BREAKER" in error


def _is_worker_terminal_abandon_result(data):
    """Whether execute_workers reached an irreversible durable terminal state."""
    if not isinstance(data, dict):
        return False
    return (
        data.get("action") == "abandon_generation"
        and data.get("success") is not True
    )


def _is_worker_operator_shutdown_interrupted(data, checkpoint):
    """Validate the complete attempt-neutral Worker shutdown projection."""

    if not isinstance(data, dict) or not isinstance(checkpoint, dict):
        return False
    if not (
        data.get("error") == "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED"
        and data.get("success") is False
        and data.get("failure_class") == "operator_shutdown"
        and data.get("action") == "retry_same_tool"
        and data.get("pending") is True
        and data.get("shutdown_requested") is True
        and data.get("checkpoint_preserved") is True
        and data.get("attempt_consumed") is False
        and data.get("attempt_neutral_persisted") is True
        and data.get("workflow_run_id")
        == checkpoint.get("workflow_run_id")
    ):
        return False
    for field in ("lease_epoch", "claimed_attempt", "restored_attempt", "max_attempts"):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            return False
    claimed = int(data["claimed_attempt"])
    restored = int(data["restored_attempt"])
    return bool(
        isinstance(data.get("effect_id"), str)
        and data.get("effect_id")
        and int(data["lease_epoch"]) >= 1
        and claimed >= 1
        and restored == claimed - 1
        and int(data["max_attempts"]) >= claimed
    )


def _worker_terminal_abandon_reason(data):
    error = str(data.get("error") or "")
    if error == "WORKER_INFRASTRUCTURE_EXHAUSTED":
        return "worker_infrastructure_exhausted"
    if error == "WORKER_WORKFLOW_ABANDONED":
        # The Worker journal's durable abandon reason is the persisted terminal
        # reason (self-verifying on read and bound to the strict-authority fence)
        # -- NOT provider text.  The router phase (tool_planning_worker_phases)
        # forwards it as worker_abandon_reason, so that the strict-authority
        # fence can reproduce the exact tombstone originally written by the
        # worker executor.  Falls back to the abstract routing constant only if
        # the worker result omitted the field.  MUST be bound to the SAME limit
        # the tombstone writers and the completed-abandon proof use
        # (_TERMINAL_REASON_MAX_CHARS = 1000, tool_bot_management): a smaller cap
        # truncates a long executor reason below the persisted tombstone length
        # and makes the completed-abandon outer-reason proof irreproducible.
        return str(data.get("worker_abandon_reason") or "worker_workflow_abandoned")[
            :1000
        ]
    return "worker_terminal_abandon"


def _is_precommit_rework_circuit_breaker_result(data):
    if not isinstance(data, dict):
        return False
    return str(data.get("error") or "") == "PRECOMMIT_REWORK_CIRCUIT_BREAKER"


def _is_official_rework_circuit_breaker_result(data):
    if not isinstance(data, dict):
        return False
    return str(data.get("error") or "") == "OFFICIAL_REWORK_CIRCUIT_BREAKER"


def _is_crossover_incompatible_result(data):
    if not isinstance(data, dict):
        return False
    return str(data.get("error") or "") == "CROSSOVER_INCOMPATIBLE"


def _is_crossover_llm_exhausted_result(data):
    if not isinstance(data, dict):
        return False
    return str(data.get("error") or "") == "CROSSOVER_LLM_EXHAUSTED"


def _is_master_ensemble_pending_retry(data, checkpoint):
    """Validate the complete journaled-Master join partition before retry."""

    if not isinstance(data, dict) or not isinstance(checkpoint, dict):
        return False
    if not (
        data.get("error") == "MASTER_ENSEMBLE_PROVIDER_PARKED"
        and data.get("pending") is True
        and data.get("action") == "retry_same_tool"
        and data.get("checkpoint_preserved") is True
        and data.get("abandoned") is False
        and data.get("needs_attention") is False
    ):
        return False
    master_slots = (
        "proposal:mechanism",
        "proposal:counterfactual",
        "proposal:compute_memory",
        "ballot:falsification",
        "ballot:scope",
    )
    accepted = data.get("accepted_slots")
    pending = data.get("pending_slots")
    slot = data.get("slot")
    if (
        not isinstance(accepted, list)
        or not isinstance(pending, list)
        or any(not isinstance(item, str) for item in accepted + pending)
        or len(set(accepted)) != len(accepted)
        or len(set(pending)) != len(pending)
        or set(accepted) & set(pending)
        or set(accepted) | set(pending) != set(master_slots)
        or slot not in pending
    ):
        return False
    role_attempt = data.get("role_attempt")
    if (
        isinstance(role_attempt, bool)
        or not isinstance(role_attempt, int)
        or role_attempt < 1
        or role_attempt >= 3
    ):
        return False
    try:
        retry_after = float(data.get("retry_after_sec"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(retry_after) or not 5.0 <= retry_after <= 60.0:
        return False
    try:
        from strict_authority_workflow import authority_run_id

        expected_run_id = authority_run_id(checkpoint.get("workflow_run_id"))
    except Exception:
        return False
    return data.get("authority_run_id") == expected_run_id
