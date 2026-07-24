"""national_tcp_policy_v1 LLM-driven evolution orchestrator.

Usage (standalone CLI):
    python web/core/orchestrator.py              # Run continuous evolution
    python web/core/orchestrator.py --one-gen    # Run one generation then stop
    python web/core/orchestrator.py --dry-run    # Only check status, no changes

Usage (from dashboard/backend/app.py):
    from orchestrator import orchestrator_loop
    await orchestrator_loop(web_ui, no_daemon=False)
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_agent_sdk import (
    query as claude_query,
    ClaudeAgentOptions,
    AssistantMessage,
    UserMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    CLINotFoundError,
    ProcessError,
    ClaudeSDKError,
)

from bot_namespace import ARCHIVED_VERSION_HIGH_WATER, FIRST_STRICT_POLICY_VERSION, bot_relpath
from tools import evolution_server, inject_ui
from llm_query import TERMINAL_ABANDON_RESULT_OWNER_TOOLS
from llm_failure import is_llm_infra_error, is_shutdown_cancel_error as _is_shutdown_cancel_error
from llm_availability import (
    LLMAvailabilityBlocked,
    LLMAvailabilityTrace,
    looks_like_provider_error_envelope,
)
from llm_availability_store import (
    LLMAvailabilityPauseError,
    active_llm_pause,
    blocked_from_pause_state,
    consume_operator_resume_ack_from_env,
    load_llm_pause,
    pause_wait_seconds,
    persist_llm_pause,
)
from shutdown_manager import ShutdownManager
from system_log import log_system_event, set_ui as set_system_log_ui
from failure_classification import INFRA_BLOCKER_REASONS
from evaluation_contract import evaluate_head_drift
from blocking_runtime import run_blocking_isolated
from orchestrator_cost_policy import (
    CostPolicyConfigurationError,
    GenerationCostPolicy,
    OperatorGenerationCostLimitExceeded,
    activate_generation_cost_scope,
    assert_operator_cost_limit_available,
    claim_generation_cost_notice,
    configure_runtime_cost_policy,
    current_generation_cost_scope,
    deactivate_generation_cost_scope,
    generation_cost_status,
    generation_identity,
    load_operator_generation_cost_policy,
    record_generation_cost,
    sdk_result_event_id,
)
import logging

log = logging.getLogger("pok.orchestrator")
os.environ.setdefault("POK_WORKFLOW_PROFILE", "national_native")
SHUTDOWN_CANCEL_COST = -99998.0
ORCH_ACTIONABLE_HANDOFF_COST = -99997.0
ORCH_OPERATOR_COST_LIMIT_COST = -99996.0
ORCH_LLM_AVAILABILITY_BLOCKED_COST = -99995.0
ORCH_RECOVERY_BLOCKED_COST = -99994.0
ORCH_GENERATION_ABANDONED_COST = -99993.0
ORCH_OPERATOR_ACTION_REQUIRED_COST = -99992.0
ORCH_ACCOUNTING_BLOCKED_COST = -99991.0
ORCH_CONSECUTIVE_ABANDON_LIMIT_COST = -99990.0
MAX_CONSECUTIVE_CANONICAL_ABANDONS = 3
_STARTUP_RECOVERY_UNSET = object()


def _canonical_abandon_proof_identity(proof):
    """Return the exact identity carried by a previously re-proven abandon.

    A bare ``ORCH_GENERATION_ABANDONED_COST`` is deliberately not authority to
    prepare another workflow.  The producer path must have run
    ``validate_completed_abandon_handoff`` and carry its compact proof forward
    to the outer-loop handoff.  Keep the structural check local as a second
    boundary before a scheduler decision or a UI event consumes the proof.
    """

    if not isinstance(proof, dict):
        return None

    def digest(value):
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        )

    identity = proof.get("checkpoint_identity")
    if (
        not digest(proof.get("transaction_id"))
        or not digest(proof.get("abandon_receipt_digest"))
        or not digest(proof.get("finalize_receipt_digest"))
        or not isinstance(identity, dict)
        or not digest(identity.get("digest"))
        or not isinstance(identity.get("workflow_run_id"), str)
        or not identity["workflow_run_id"].strip()
        or type(identity.get("next_v")) is not int
        or type(identity.get("source_v")) is not int
        or type(identity.get("checkpoint_revision")) is not int
        or identity["checkpoint_revision"] < 1
        or not isinstance(identity.get("stage"), str)
        or not identity["stage"].strip()
    ):
        return None
    return dict(identity)


def _remember_verified_canonical_abandon(gen_ctx, proof) -> bool:
    """Bind an already-validated terminal proof to this in-memory cycle only."""

    identity = _canonical_abandon_proof_identity(proof)
    if identity is None or gen_ctx is None:
        return False
    if (
        getattr(gen_ctx, "next_v", None) != identity["next_v"]
        or getattr(gen_ctx, "source_v", None) != identity["source_v"]
    ):
        return False
    # Serialize before retaining it so an SDK/result object cannot mutate the
    # event proof after the strict handoff validator returned.
    try:
        retained = json.loads(json.dumps(proof, sort_keys=True))
    except (TypeError, ValueError):
        return False
    setattr(gen_ctx, "_verified_canonical_abandon_proof", retained)
    return True


def _remembered_canonical_abandon_proof(gen_ctx):
    """Read only a complete proof remembered by this exact generation cycle."""

    proof = getattr(gen_ctx, "_verified_canonical_abandon_proof", None)
    return proof if _canonical_abandon_proof_identity(proof) is not None else None

# Infra-only blocker reasons used by the timed_out handler to distinguish
# scheduler/daemon failures from real bot regressions.
_INFRA_BLOCKER_REASONS_SET = frozenset(INFRA_BLOCKER_REASONS)

_TERMINAL_ABANDON_RESULT_OWNER_TOOLS = TERMINAL_ABANDON_RESULT_OWNER_TOOLS


def _normalized_provider_tool_name(name) -> str:
    value = str(name or "")
    return value.rsplit("__", 1)[-1]


def _write_timeout_checkpoint_from_exact_snapshot(
    checkpoint: dict,
    stage: str,
    **updates,
) -> bool:
    """CAS one timeout transition against the exact checkpoint observed.

    Provider cancellation cannot stop an already-running ThreadPool evaluation.
    Every timeout overlay therefore carries the workflow, revision, and stage
    observed after cancellation.  A late evaluator that advances the checkpoint
    wins; the stale timeout must not overwrite that newer state.
    """

    workflow_run_id = checkpoint.get("workflow_run_id")
    revision = checkpoint.get("checkpoint_revision")
    current_stage = checkpoint.get("stage")
    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    if (
        not isinstance(workflow_run_id, str)
        or not workflow_run_id.strip()
        or type(revision) is not int
        or revision < 1
        or not isinstance(current_stage, str)
        or not current_stage
        or type(next_v) is not int
        or type(source_v) is not int
    ):
        return False

    from evolution_core import write_pipeline_checkpoint

    return bool(write_pipeline_checkpoint(
        next_v,
        source_v,
        stage,
        expected_checkpoint_revision=revision,
        expected_checkpoint_stage=current_stage,
        expected_workflow_run_id=workflow_run_id,
        **updates,
    ))


_LLM_AVAILABILITY_CONTROL_ERRORS = frozenset({
    "LLM_AVAILABILITY_STATE_INVALID",
    "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
    "WORKER_AVAILABILITY_DEFER_FAILED",
    "WORKER_AVAILABILITY_RESUME_FAILED",
    "WORKER_AVAILABILITY_RESUME_INVARIANT_FAILED",
    "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
})


def _resolve_daemon_workers(value: int | None) -> int:
    """Resolve the orchestrator default through daemon resource authority."""

    if value is not None:
        return int(value)
    from daemon_management import default_daemon_workers

    return default_daemon_workers()


def _tool_result_payload(value):
    """Decode the SDK's string-or-content-block ToolResult shape."""

    if isinstance(value, dict):
        if "error" in value or "action" in value or "success" in value:
            return value
        for key in ("text", "content"):
            if key in value:
                decoded = _tool_result_payload(value.get(key))
                if decoded:
                    return decoded
        return {}
    if isinstance(value, list):
        for item in value:
            decoded = _tool_result_payload(item)
            if decoded:
                return decoded
        return {}
    if isinstance(value, str):
        try:
            return _tool_result_payload(json.loads(value))
        except (TypeError, json.JSONDecodeError):
            return {}
    return {}


def _completed_abandon_tool_result(value):
    """Find one exact canonical-abandon result inside an SDK tool result."""

    required = {
        "abandoned",
        "cleared_checkpoint",
        "workflow_run_id",
        "abandon_transaction_id",
        "abandon_receipt_digest",
        "finalize_receipt_digest",
        "abandon_checkpoint_identity",
    }
    matches = []

    def collect(candidate):
        if isinstance(candidate, dict):
            if required.issubset(candidate):
                matches.append(candidate)
            for key in ("abandon_result", "result", "content", "text"):
                if key in candidate:
                    collect(candidate.get(key))
            return
        if isinstance(candidate, list):
            for item in candidate:
                collect(item)
            return
        if isinstance(candidate, str):
            try:
                collect(json.loads(candidate))
            except (TypeError, json.JSONDecodeError):
                pass

    collect(value)
    return matches[0] if len(matches) == 1 else None


def _raise_for_llm_availability_tool_result(content) -> None:
    """Turn a durable Worker pause result back into local stream control."""

    payload = _tool_result_payload(content)
    error = str(payload.get("error") or "")
    if error == "LLM_AVAILABILITY_BLOCKED":
        state = payload.get("availability") or active_llm_pause()
        if isinstance(state, dict) and state.get("active"):
            raise blocked_from_pause_state(state, role="Orchestrator")
        raise LLMAvailabilityPauseError(
            "Worker reported LLM availability blocked without a valid durable pause"
        )
    if error in _LLM_AVAILABILITY_CONTROL_ERRORS:
        raise LLMAvailabilityPauseError(
            f"Worker LLM availability control failed closed: {error}"
        )


async def _honor_active_llm_pause(ui=None, shutdown_mgr=None) -> bool:
    """Return whether an LLM call may proceed under the durable pause policy.

    Deterministic checkpoint routes call this only after they have had a chance
    to advance.  A manual billing/auth pause stops the orchestrator without a
    retry loop.  Transient availability records wait only until their bounded
    system-owned cooldown and then reconcile themselves.
    """

    state = active_llm_pause()
    if not state:
        return True
    category = str(state.get("category") or "unknown")
    evidence_digest = str(state.get("evidence_digest") or "")
    wait = pause_wait_seconds(state)
    if wait is None:
        msg = (
            f"LLM availability is manually paused ({category}); checkpoint and "
            "Worker attempt are preserved. Restart with "
            f"POK_LLM_RESUME_EVIDENCE_DIGEST={evidence_digest} only after the "
            "provider account/credential condition is resolved."
        )
        if ui:
            ui.log_history(msg, "error")
            ui.set_status(f"Stopped: LLM unavailable ({category})", is_working=False)
        log.error(msg)
        try:
            log_system_event(
                "orchestrator.llm_availability_paused",
                "error",
                msg,
                {
                    "category": category,
                    "evidence_digest": evidence_digest,
                    "retry_policy": state.get("retry_policy"),
                    "operator_action_required": True,
                },
            )
        except Exception:
            pass
        return False

    wait = max(0.0, float(wait))
    if wait > 0:
        msg = (
            f"LLM availability cooldown active ({category}); retrying after "
            f"{wait:.0f}s without consuming a generation/Worker attempt."
        )
        if ui:
            ui.log_history(msg, "warn")
            ui.set_status(f"LLM cooldown ({category})", is_working=False)
        log.warning(msg)
        try:
            log_system_event(
                "orchestrator.llm_availability_cooldown",
                "warn",
                msg,
                {
                    "category": category,
                    "evidence_digest": evidence_digest,
                    "wait_seconds": round(wait, 3),
                    "operator_action_required": False,
                },
            )
        except Exception:
            pass
        if shutdown_mgr:
            try:
                await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=wait)
                return False
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(wait)
    return active_llm_pause() is None


def _is_cycle_infra_error(e, *, is_shutting_down: bool = False) -> bool:
    """Classify an orchestrator exception as LLM-infra (short -0.5 backoff) vs real
    business/auth failure. Type-based via is_llm_infra_error + keyword fallback for
    SDK ProcessError/exit-143 wrapping and signature field errors."""
    if is_shutting_down and _is_shutdown_cancel_error(e):
        return False
    err_str = str(e).lower()
    return (is_llm_infra_error(e)
            or "signature" in err_str
            or "missing required field" in err_str
            or "exit code 143" in err_str
            or "command failed with exit code" in err_str
            or "processerror" in err_str
            or "claude code returned an error result" in err_str)  # root-cause-audit 2026-06-21: SDK query.py:852 裸 Exception


class _OrchSignatureRetryable(Exception):
    """Internal signal: the orchestrator's own SDK stream hit a transient
    signature-field deserialization error (claude_agent_sdk 0.2.91) AND no MCP
    tool had executed yet, so a fresh stream is safe (no side-effects to
    duplicate). Caught by the retry loop in _run_one_cycle.

    root-cause-audit 2026-06-17: sub-agents already retry signature errors via
    _run_stream_with_signature_retry (llm_query.py), but the orchestrator's own
    claude_query stream had no retry — every signature error failed the whole
    cycle (-0.5 backoff + session clear). Confirmed NOT caused by adaptive
    thinking (disabled mode had 427 errors vs adaptive 70); this is an SDK
    stream bug, so a bounded retry is the correct mitigation until an SDK fix.
    """


class _OrchFirstActivityTimeout(Exception):
    """Internal signal: orchestrator LLM produced no first stream message quickly.

    This is different from a normal long generation: no AssistantMessage means
    no session id has been saved yet, so the checkpoint watchdog cannot observe
    or recover the stuck stream. Treat it like infra and retry from checkpoint.
    """


class _OrchActionableStageTimeout(Exception):
    """Internal signal: checkpoint is waiting at a deterministic next-tool stage.

    Some SDK sessions keep the stream open after an MCP tool returns a blocking
    gate result such as ``quality_failed``. Waiting for the full cycle timeout
    makes recovery effectively unavailable. When the persisted checkpoint has
    already recorded an actionable route for long enough, cancel the stream and
    resume from the checkpoint in a fresh cycle.
    """


class _OrchActionableStageHandoff(Exception):
    """Internal signal: an MCP tool just reached a deterministic next-tool stage.

    This is normal pipeline control flow, not SDK/LLM infrastructure failure.
    The current SDK stream is disposable once the checkpoint has recorded the
    canonical next route; the outer loop should yield immediately and route from
    the checkpoint without backoff or error telemetry.
    """

    def __init__(self, message: str, handoff: dict | None = None):
        super().__init__(message)
        self.handoff = dict(handoff or {})


class _OrchStreamStallTimeout(Exception):
    """Internal signal: orchestrator main-agent stream stalled mid-conversation.

    The deepseek-v4-pro endpoint behind cc-switch intermittently stalls after
    producing some output (a tool_use whose tool_result never returns, or the
    model simply stops streaming). The sub-role path (run_claude_query) handles
    this with the stall_timeout layer (Fix C). The orchestrator main agent,
    however, polls via _await_next_stream_message and only aborts on an
    "actionable stage" — which requires a checkpoint. Early in a cycle (e.g.
    before any tool runs, or between MCP handoffs) there is NO checkpoint, so a
    stalled main-agent stream would otherwise wait the full CYCLE_TIMEOUT
    (5400s = 90min) before any recovery. Treat a long mid-stream silence as
    infra, cancel the stream, and let the outer loop retry the cycle (checkpoint
    is preserved where one exists).
"""



# Module-level flag set by the watchdog coroutine when it detects a stuck pipeline.
# The main orchestrator_loop checks this flag at the top of each iteration and forces
# a fresh _run_one_cycle (discarding the stale session) when set.
_watchdog_triggered = False
_orchestrator_provider_stream_active = False
ORCH_FIRST_ACTIVITY_TIMEOUT = int(os.environ.get("POK_ORCH_FIRST_ACTIVITY_TIMEOUT", "600"))
ORCH_STREAM_POLL_INTERVAL = float(os.environ.get("POK_ORCH_STREAM_POLL_INTERVAL", "15"))
ORCH_ACTIONABLE_STAGE_TIMEOUT = float(os.environ.get("POK_ORCH_ACTIONABLE_STAGE_TIMEOUT", "300"))
# D (2026-07-09): generic mid-stream stall ceiling for the orchestrator main
# agent. Unlike _detect_actionable_stage_stall (which needs a checkpoint), this
# fires when the main stream is silent and no current-generation tool/sub-role
# progress is visible, so a stalled main-agent stream does not wait the full
# CYCLE_TIMEOUT (5400s). Disabled when <= 0 (then only actionable-stage +
# CYCLE_TIMEOUT bound the wait).
ORCH_STREAM_STALL_TIMEOUT = float(os.environ.get("POK_ORCH_STREAM_STALL_TIMEOUT", "300"))
ORCH_EXTERNAL_PROGRESS_TAIL_BYTES = int(os.environ.get("POK_ORCH_EXTERNAL_PROGRESS_TAIL_BYTES", "524288"))
# One native 70-hand operation can legitimately consume the frozen
# local-strength envelope: 300 s capacity wait + 60 s bounded read-only
# artifact preparation + 120 s startup + 5,415 s engine + 35 s cleanup +
# 30 s durable completion/replay projection = 5,960 s.  This is a single,
# absolute extension ceiling for a
# checkpoint-bound match, never a rolling heartbeat lease or a blanket
# increase to CYCLE_TIMEOUT.  The frozen sidecar phase deadline remains the
# authoritative lower cap.
ORCH_NATIVE_MATCH_MAX_EXTENSION_HARD_CAP_SEC = 5_960.0
ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC = max(
    0.0,
    min(
        ORCH_NATIVE_MATCH_MAX_EXTENSION_HARD_CAP_SEC,
        float(os.environ.get("POK_ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC", "5960")),
    ),
)
ORCH_NATIVE_MATCH_PROGRESS_MAX_AGE_SEC = 90.0
ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC = 5.0
POST_GENERATION_CLEANUP_TIMEOUT = int(os.environ.get("POK_POST_GENERATION_CLEANUP_TIMEOUT", "900"))
RUNTIME_BRANCH_GUARD_INTERVAL = float(os.environ.get("POK_RUNTIME_BRANCH_GUARD_INTERVAL", "5"))
STABILITY_OBSERVATION_MAINTENANCE_INTERVAL = float(
    os.environ.get("POK_STABILITY_OBSERVATION_MAINTENANCE_INTERVAL", "5")
)

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def _render_orchestrator_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    if not isinstance(inputs, dict) or set(inputs) != {"context", "dry_run"}:
        raise ValueError("Orchestrator renderer input contract mismatch")

    template = (
        Path(__file__).resolve().parent / "prompts" / "orchestrator.md"
    ).read_text(encoding="utf-8")
    prompt = template.replace("{context}", str(inputs["context"]))
    if bool(inputs.get("dry_run")):
        prompt += (
            "\n\nIMPORTANT: This is a DRY RUN. Do not call any tool. Report only "
            "the status already supplied in the context; live rating, H2H, match "
            "history, and bot-stat query tools are intentionally unavailable."
        )
    from strategy_reference_pack import current_strict_runtime_prompt_overlay

    prompt += "\n\n" + current_strict_runtime_prompt_overlay()
    return LLMRenderedMaterial(
        text=prompt,
        evidence_kind="checkpoint_context_projection",
        evidence_provenance={
            "context_digest": hashlib.sha256(
                str(inputs["context"]).encode("utf-8")
            ).hexdigest(),
            "dry_run": bool(inputs["dry_run"]),
        },
    )

# Tools the Orchestrator legitimately calls many times per cycle (exploration,
# file reads, task bookkeeping). Excluded from the redundant-call warning's
# strict threshold so the warning keeps signal (root-cause fix for the
# pipeline.redundant_tool_call noise storm: 264 events/cycle post-restart,
# almost all Bash/Read/TaskUpdate that are normal multi-step work). Pipeline
# MCP tools (run_master, run_crossover, commit_bot, ...) stay strict — those
# should fire ~once per cycle, so any repeat is a real anomaly.
_NOISY_TOOLS = frozenset({
    "Bash", "Read", "Grep", "Glob", "LS",
    "TaskCreate", "TaskUpdate", "TaskOutput", "TaskList", "TaskGet",
    "Edit", "Write", "NotebookEdit",
})
_REDUNDANT_NOISY_THRESHOLD = 6    # noisy tools: warn once at the 6th call
_REDUNDANT_STRICT_THRESHOLD = 2   # pipeline tools: warn once at the 2nd call

_ORCH_EXTERNAL_PROGRESS_EVENT_TYPES = frozenset({
    "pipeline.llm_role_first_activity",
    "pipeline.llm_role_first_activity_delayed",
    "pipeline.llm_role_progress",
    "pipeline.master_checkpoint_heartbeat",
    "pipeline.orchestrator_native_match_extension_granted",
})

from orchestrator_context import _build_context, _make_precompact_hook, _make_bot_dir_guard_hook, set_cycle_start_time  # noqa: E402
from orchestrator_session import (  # noqa: E402
    _rotate_orchestrator_logs, _is_rate_limited,
    _save_orchestrator_session, _load_orchestrator_session, _clear_orchestrator_session,
)
from evolution_infra import find_current_v  # noqa: E402
from llm_query import (  # noqa: E402
    LLMProviderCleanupError,
    activate_owned_provider_attempt,
    await_provider_stream_next_bounded,
    bind_llm_role_provider_prompt,
    cancel_provider_stream_task_bounded,
    cleanup_owned_provider_attempt,
    create_owned_provider_attempt,
    extract_result_error,
    mark_owned_provider_attempt_unresolved,
    owned_provider_attempt_exit_confirmed,
    owned_provider_attempt_scope,
    owned_provider_attempt_transport,
    reset_owned_provider_attempt,
)


def _bind_generation_cost_runtime(
    checkpoint,
    *,
    gen_ctx=None,
    ui=None,
    policy: GenerationCostPolicy | None = None,
):
    """Bind durable cost accounting to the system-owned workflow identity."""

    selected_policy = policy or load_operator_generation_cost_policy()
    configure_runtime_cost_policy(selected_policy)
    scope = activate_generation_cost_scope(
        generation_identity(checkpoint, gen_ctx),
        selected_policy,
    )
    status = generation_cost_status(scope)
    receipt = scope.receipt(
        spent_before_usd=float(status.get("spent_usd") or 0.0),
        ledger_errors=tuple(status.get("accounting_errors") or ()),
    )
    begin_cost = getattr(ui, "begin_generation_cost", None) if ui else None
    if callable(begin_cost):
        begin_cost(scope.generation_id, status.get("spent_usd", 0.0), receipt)
    if claim_generation_cost_notice(scope, "policy_bound"):
        severity = "info" if status.get("accounting_ok") else "warn"
        log_system_event(
            "pipeline.generation_cost_policy_bound",
            severity,
            (
                f"Generation cost policy bound for {scope.generation_id}: "
                f"{selected_policy.enforcement_mode}"
            ),
            {
                **receipt,
                "accounting_ok": status.get("accounting_ok"),
                "spent_usd": status.get("spent_usd"),
            },
        )
    return scope


def _check_generation_cost_policy(ui=None):
    """Warn in default mode; stop only for the explicit operator hard limit."""

    scope = current_generation_cost_scope()
    if scope is None:
        return {"active": False}
    status = generation_cost_status(scope)
    if status.get("warning_reached") and claim_generation_cost_notice(scope, "warning"):
        spent = float(status.get("spent_usd") or 0.0)
        warning = float(status.get("warning_usd") or 0.0)
        msg = (
            f"Generation {scope.generation_id} LLM spend reached ${spent:.2f} "
            f"(monitoring threshold ${warning:.2f}); evolution continues."
        )
        log.warning(msg)
        if ui:
            ui.log_history(f"[Orchestrator] {msg}", "warn")
        log_system_event(
            "pipeline.generation_cost_warning",
            "warn",
            msg,
            {
                **status,
                "directive": "Telemetry only; no generation stop was requested by the operator.",
            },
        )
    if (
        not status.get("accounting_ok")
        and scope.policy.hard_limit_usd is None
        and claim_generation_cost_notice(scope, "accounting_warning")
    ):
        errors = [str(item) for item in status.get("accounting_errors") or ()]
        msg = (
            f"Generation {scope.generation_id} cost accounting is incomplete/unknown; "
            "monitor-only evolution continues."
        )
        log.warning("%s errors=%s", msg, errors[:5])
        if ui:
            ui.log_history(f"[Orchestrator] {msg}", "warn")
        binding = scope.receipt(
            spent_before_usd=float(status.get("spent_usd") or 0.0),
            ledger_errors=tuple(errors),
        )
        log_system_event(
            "pipeline.generation_cost_accounting_warning",
            "warn",
            msg,
            {
                **status,
                "policy_binding": binding,
                "directive": (
                    "Telemetry is incomplete; monitor-only mode continues. "
                    "Inspect ledger_errors before interpreting the displayed USD total."
                ),
            },
        )
        # update_cost($0) cannot communicate an unknown USD amount.  Push the
        # bound receipt so SSE/state consumers see ledger_errors immediately.
        _project_generation_cost_runtime(ui)
    try:
        return assert_operator_cost_limit_available(scope)
    except OperatorGenerationCostLimitExceeded as exc:
        status = dict(exc.status or status)
        if claim_generation_cost_notice(scope, "operator_hard_limit_tripped"):
            msg = f"Operator generation cost limit stopped {scope.generation_id}: {exc}"
            log.error(msg)
            if ui:
                ui.log_history(f"[Orchestrator] {msg}", "error")
            log_system_event(
                "pipeline.operator_generation_cost_limit_tripped",
                "error",
                msg,
                {
                    **status,
                    "policy_binding": scope.receipt(
                        spent_before_usd=float(status.get("spent_usd") or 0.0),
                        ledger_errors=tuple(status.get("accounting_errors") or ()),
                    ),
                    "operator_action_required": True,
                    "directive": (
                        "Change or disable the parent-process operator limit, then explicitly restart. "
                        "The checkpoint is preserved."
                    ),
                },
            )
        raise


def _project_generation_cost_runtime(ui=None) -> dict:
    """Refresh dashboard state from the durable ledger without rebinding."""

    scope = current_generation_cost_scope()
    status = generation_cost_status(scope)
    begin_cost = getattr(ui, "begin_generation_cost", None) if ui else None
    if scope is not None and callable(begin_cost):
        begin_cost(
            scope.generation_id,
            status.get("spent_usd", 0.0),
            scope.receipt(
                spent_before_usd=float(status.get("spent_usd") or 0.0),
                ledger_errors=tuple(status.get("accounting_errors") or ()),
            ),
        )
    return status


_CORRECTIVE_RETRY_STAGES_BY_TOOL = {
    "execute_workers": {
        "quality_failed",
        "reviewed",
        "critic_checked",
        "precommit_failed",
        "official_failed",
        "repair_planned",
        "rework_running",
    },
    "run_quality_gates": {"workers_done"},
    "run_review": {"quality_passed"},
    "run_critic": {"reviewed", "critic_checked"},
    "run_precommit_eval": {"critic_checked"},
}


def _as_positive_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _has_recorded_gate_failure(checkpoint) -> bool:
    gate_results = checkpoint.get("gate_results") if isinstance(checkpoint, dict) else None
    if not isinstance(gate_results, dict):
        return False
    for gate in gate_results.values():
        if not isinstance(gate, dict):
            continue
        if gate.get("passed") is False or gate.get("ok") is False or gate.get("success") is False:
            return True
    return False


def _has_corrective_retry_history(checkpoint) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    return bool(
        _as_positive_int(checkpoint.get("audit_attempt")) > 0
        or _as_positive_int(checkpoint.get("generation_attempt")) > 0
        or _as_positive_int(checkpoint.get("precommit_attempt")) > 0
        or _as_positive_int(checkpoint.get("worker_failure_count")) > 0
        or checkpoint.get("reviewer_feedback")
        or checkpoint.get("audit_context")
        or _has_recorded_gate_failure(checkpoint)
    )


def _read_checkpoint_for_repeated_tool_guard():
    try:
        from evolution_infra import read_pipeline_checkpoint
        return read_pipeline_checkpoint() or {}
    except Exception:
        return {}


def _route_allows_tool(checkpoint, tool_name: str) -> bool:
    try:
        from pipeline_state import route_policy
        route = route_policy(checkpoint)
    except Exception:
        return True
    return route.get("next_tool") == tool_name


def _classify_allowed_repeated_pipeline_tool(tool_name: str, tool_input=None):
    """Return an info payload when a repeated MCP tool call is valid state flow.

    The outer Orchestrator stream sees only "this is the 2nd run_master call".
    Whether that is wasteful or correct depends on the persisted checkpoint:
    Master validation/audit rejection, quality repair, review repair, and
    precommit repair all intentionally re-enter a previously used pipeline tool
    in the same cycle. Keep redundant-call warnings for repeats that are not on
    one of those explicit state-machine routes.
    """
    if tool_name in _NOISY_TOOLS:
        return None

    checkpoint = _read_checkpoint_for_repeated_tool_guard()
    if not checkpoint:
        return None
    stage = checkpoint.get("stage")

    if tool_name == "run_master":
        master_plan = checkpoint.get("master_plan")
        has_plan = bool(master_plan) if not isinstance(master_plan, list) else bool(master_plan)
        audit_context = checkpoint.get("audit_context")
        if (
            stage == "direction_audited"
            and not has_plan
            and _route_allows_tool(checkpoint, "run_master")
            and (
                _as_positive_int(checkpoint.get("audit_attempt")) > 0
                or bool(audit_context)
            )
        ):
            return {
                "reason": "corrective_master_replan",
                "stage": stage,
                "audit_attempt": _as_positive_int(checkpoint.get("audit_attempt")),
                "next_v": checkpoint.get("next_v"),
                "source_v": checkpoint.get("source_v"),
            }
        return None

    allowed_stages = _CORRECTIVE_RETRY_STAGES_BY_TOOL.get(tool_name)
    if not allowed_stages or stage not in allowed_stages:
        return None
    if not _route_allows_tool(checkpoint, tool_name):
        return None
    if not _has_corrective_retry_history(checkpoint):
        return None

    return {
        "reason": "corrective_gate_reentry",
        "stage": stage,
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "generation_attempt": _as_positive_int(checkpoint.get("generation_attempt")),
        "precommit_attempt": _as_positive_int(checkpoint.get("precommit_attempt")),
        "worker_failure_count": _as_positive_int(checkpoint.get("worker_failure_count")),
    }


_DETERMINISTIC_RECOVERY_TOOLS = frozenset({
    "abandon_generation",
    "execute_workers",
    "prepare_next_gen",
    "run_crossover",
    "run_quality_gates",
    "run_review",
    "run_critic",
    "run_precommit_eval",
    "commit_bot",
    "run_archivist",
})

_DETERMINISTIC_ROUTES_WITH_LLM = frozenset({
    "run_crossover",
    "run_direction_audit",
    "run_master",
    "execute_workers",
    "run_review",
    "run_critic",
    "run_archivist",
})


def _deterministic_route_requires_llm(checkpoint, next_tool: str) -> bool:
    if next_tool not in _DETERMINISTIC_ROUTES_WITH_LLM:
        return False
    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        system_migration = is_declared_native_bootstrap(checkpoint)
    except Exception:
        system_migration = False
    if not system_migration:
        return True
    stage = str((checkpoint or {}).get("stage") or "")
    # These exact first-migration stages are content-bound system verifiers.
    if next_tool == "run_direction_audit" and stage == "prepared":
        return False
    if next_tool == "execute_workers" and stage == "master_planned" and not (
        (checkpoint or {}).get("reviewer_feedback")
    ):
        return False
    # Master (three proposals + two anonymous ballots), Review, and Critic are
    # mandatory LLM governance stages even for the deterministic first Worker
    # migration.  Only Direction's exact receipt and the initial Worker
    # blueprint are system-executable while the provider is paused.
    return True


def _resolve_recovery_route(checkpoint):
    """Return deterministic checkpoint route only for known recovery-safe tools."""
    if not checkpoint:
        return None
    try:
        from pipeline_state import route_policy
        route = route_policy(checkpoint)
    except Exception:
        return None
    next_tool = route.get("next_tool")
    if next_tool in {"run_direction_audit", "run_master"}:
        try:
            from system_strict_bootstrap import system_recovery_eligible

            if not system_recovery_eligible(checkpoint, next_tool):
                return None
        except Exception:
            return None
    elif next_tool not in _DETERMINISTIC_RECOVERY_TOOLS:
        return None
    return {
        "next_tool": next_tool,
        "directive": route.get("directive"),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": checkpoint.get("stage"),
        "parent2_v": checkpoint.get("parent2_v"),
        "route": route,
    }


def _checkpoint_master_plan_arg(checkpoint):
    """Return the saved plan context for deterministic review/critic routes."""
    if not isinstance(checkpoint, dict):
        return []
    master_plan = checkpoint.get("master_plan")
    if isinstance(master_plan, (dict, list)):
        return master_plan
    return []


def _checkpoint_reviewer_feedback(checkpoint):
    if not isinstance(checkpoint, dict):
        return ""
    feedback = checkpoint.get("reviewer_feedback")
    if isinstance(feedback, str) and feedback.strip():
        return feedback
    gate = (checkpoint.get("gate_results") or {}).get("review") or {}
    feedback = gate.get("feedback")
    return feedback if isinstance(feedback, str) else ""


def _checkpoint_commit_strategy(checkpoint):
    if not isinstance(checkpoint, dict):
        return ""
    master_plan = checkpoint.get("master_plan") or {}
    if isinstance(master_plan, dict) and master_plan.get("strategy"):
        return str(master_plan.get("strategy"))
    return "crossover" if checkpoint.get("parent2_v") is not None else "master"


def _deterministic_route_handler_and_args(next_tool, checkpoint, next_v, source_v, parent2_v):
    """Return the MCP handler and canonical args for a deterministic checkpoint route."""
    if next_tool == "run_direction_audit":
        args = {"source_v": source_v, "next_v": next_v}
        from tool_planning import run_direction_audit
        return run_direction_audit.handler, args
    if next_tool == "run_master":
        args = {"source_v": source_v, "next_v": next_v}
        from tool_planning import run_master
        return run_master.handler, args
    if next_tool == "execute_workers":
        args = {"next_v": next_v, "source_v": source_v}
        reviewer_feedback = _checkpoint_reviewer_feedback(checkpoint)
        if reviewer_feedback:
            args["reviewer_feedback"] = reviewer_feedback
        from tool_planning import execute_workers
        return execute_workers.handler, args
    if next_tool == "prepare_next_gen":
        args = {"source_v": source_v, "next_v": next_v}
        from tool_gates import prepare_next_gen
        return prepare_next_gen.handler, args
    if next_tool == "abandon_generation":
        from tool_bot_management import abandon_generation
        return abandon_generation.handler, {}
    if next_tool == "run_crossover":
        args = {
            "parent_a": source_v,
            "parent_b": parent2_v,
            "target_v": next_v,
        }
        from tool_commit import run_crossover
        return run_crossover.handler, args
    if next_tool == "run_quality_gates":
        args = {"version": next_v, "source_v": source_v}
        from tool_gates import run_quality_gates
        return run_quality_gates.handler, args
    if next_tool == "run_review":
        args = {
            "version": next_v,
            "source_v": source_v,
            "plan": _checkpoint_master_plan_arg(checkpoint),
        }
        from tool_gates import run_review
        return run_review.handler, args
    if next_tool == "run_critic":
        args = {
            "version": next_v,
            "source_v": source_v,
            "plan": _checkpoint_master_plan_arg(checkpoint),
            "reviewer_feedback": _checkpoint_reviewer_feedback(checkpoint),
            "force_advance": False,
        }
        from tool_gates import run_critic
        return run_critic.handler, args
    if next_tool == "run_precommit_eval":
        args = {"version": next_v, "source_v": source_v}
        from tool_eval import run_precommit_eval
        return run_precommit_eval.handler, args
    if next_tool == "commit_bot":
        args = {
            "version": next_v,
            "source_v": source_v,
            "strategy": _checkpoint_commit_strategy(checkpoint),
            "review_approved": True,
        }
        from tool_commit import commit_bot
        return commit_bot.handler, args
    if next_tool == "run_archivist":
        args = {"version": next_v, "source_v": source_v}
        from tool_commit import run_archivist
        return run_archivist.handler, args
    return None, None


def _coerce_event_ts(value) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _pipeline_checkpoint_observation():
    """Read checkpoint bytes while preserving absent-vs-invalid authority."""

    try:
        from evolution_core import PIPELINE_STATE_FILE, read_pipeline_checkpoint
    except Exception as exc:
        return {
            "checkpoint": None,
            "path_exists": None,
            "error": f"checkpoint_import_failed:{type(exc).__name__}",
        }
    path_exists_before = os.path.lexists(PIPELINE_STATE_FILE)
    try:
        checkpoint = read_pipeline_checkpoint()
    except Exception as exc:
        return {
            "checkpoint": None,
            "path_exists": os.path.lexists(PIPELINE_STATE_FILE),
            "path_existed_before": path_exists_before,
            "error": f"checkpoint_read_failed:{type(exc).__name__}",
        }
    path_exists = os.path.lexists(PIPELINE_STATE_FILE)
    if checkpoint is None:
        return {
            "checkpoint": None,
            "path_exists": path_exists,
            "path_existed_before": path_exists_before,
            "error": (
                "checkpoint_disappeared_during_read"
                if path_exists_before and not path_exists
                else "checkpoint_unreadable_or_invalid"
                if path_exists
                else None
            ),
        }
    if not isinstance(checkpoint, dict):
        return {
            "checkpoint": None,
            "path_exists": path_exists,
            "path_existed_before": path_exists_before,
            "error": "checkpoint_projection_not_object",
        }
    identity_issues = []
    _IDENTITY_FLOORS = {
        "next_v": FIRST_STRICT_POLICY_VERSION,
        "source_v": ARCHIVED_VERSION_HIGH_WATER,
        "checkpoint_revision": 1,
    }
    for field, floor in _IDENTITY_FLOORS.items():
        value = checkpoint.get(field)
        if type(value) is not int or value < floor:
            identity_issues.append(field)
    for field in ("stage", "workflow_run_id"):
        value = checkpoint.get(field)
        if not isinstance(value, str) or not value.strip():
            identity_issues.append(field)
    if identity_issues:
        return {
            "checkpoint": None,
            "path_exists": path_exists,
            "path_existed_before": path_exists_before,
            "error": (
                "checkpoint_projection_identity_invalid:"
                + ",".join(identity_issues)
            ),
        }
    return {
        "checkpoint": checkpoint,
        "path_exists": path_exists,
        "path_existed_before": path_exists_before,
        "error": None,
    }


def _read_active_pipeline_checkpoint():
    observation = _pipeline_checkpoint_observation()
    checkpoint = observation.get("checkpoint")
    return checkpoint if isinstance(checkpoint, dict) else None


def _read_structured_events_tail(max_bytes=None):
    """Read a bounded tail of the canonical structured-event ledger."""
    try:
        from event_bus import _events_file
        path = _events_file()
    except Exception:
        return []
    if path is None or not path.exists():
        return []
    limit = max(4096, int(max_bytes or ORCH_EXTERNAL_PROGRESS_TAIL_BYTES))
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - limit)
            f.seek(start)
            if start > 0:
                f.readline()
            payload = f.read()
    except Exception:
        return []
    try:
        return payload.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []


def _event_matches_active_generation(event_data, checkpoint):
    if not checkpoint:
        return False
    expected_workflow_run_id = str(checkpoint.get("workflow_run_id") or "").strip()
    event_workflow_run_id = str(event_data.get("workflow_run_id") or "").strip()
    if expected_workflow_run_id:
        return event_workflow_run_id == expected_workflow_run_id

    expected_run_id = str(checkpoint.get("run_id") or "").strip()
    event_run_id = str(event_data.get("run_id") or "").strip()
    if expected_run_id:
        return event_run_id == expected_run_id

    expected_v_text = str(checkpoint.get("next_v") or "").strip()
    if not expected_v_text:
        return False
    for key in ("version", "next_v", "candidate_v", "target_v"):
        if str(event_data.get(key) or "").strip() == expected_v_text:
            return True

    log_file = str(event_data.get("log_file") or "")
    return f"/v{expected_v_text}/logs/" in log_file


def _latest_orchestrator_external_progress(since_ts):
    """Return current-generation tool/sub-role progress newer than since_ts.

    The orchestrator main stream is silent while a local MCP tool executes.
    That silence is not an SDK stall if the active checkpoint or a sub-role log
    shows current-generation progress. Background daemon events are deliberately
    ignored so ratings or async queues cannot mask a stuck generation.
    """
    since = _coerce_event_ts(since_ts)
    checkpoint = _read_active_pipeline_checkpoint()
    best = None

    if checkpoint:
        from pipeline_state import pipeline_runtime_activity_ts

        checkpoint_ts = max(
            _coerce_event_ts(checkpoint.get("last_update_ts")),
            _coerce_event_ts(checkpoint.get("last_stage_change_ts")),
            pipeline_runtime_activity_ts(checkpoint),
        )
        if checkpoint_ts > since:
            best = {
                "ts": checkpoint_ts,
                "source": "checkpoint",
                "event_type": "pipeline.checkpoint_progress",
                "next_v": checkpoint.get("next_v"),
                "stage": checkpoint.get("stage"),
            }

    if not checkpoint:
        return best

    for line in _read_structured_events_tail():
        try:
            event = json.loads(line)
        except Exception:
            continue
        event_type = str(event.get("type") or "")
        if event_type not in _ORCH_EXTERNAL_PROGRESS_EVENT_TYPES:
            continue
        ts = _coerce_event_ts(event.get("ts"))
        if ts <= since:
            continue
        data = event.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        emitter_proc = str(data.get("emitter_proc") or data.get("proc") or "")
        if emitter_proc and emitter_proc not in {"web", "orchestrator"}:
            continue
        if not _event_matches_active_generation(data, checkpoint):
            continue
        if best is None or ts > best["ts"]:
            best = {
                "ts": ts,
                "source": "system_event",
                "event_type": event_type,
                "message": str(event.get("message") or "")[:240],
                "next_v": checkpoint.get("next_v"),
                "stage": data.get("stage") or checkpoint.get("stage"),
                "role": data.get("role"),
                "log_file": data.get("log_file"),
            }
    return best


def _detect_actionable_stage_stall(timeout_sec=None):
    """Return checkpoint route data when a deterministic next-tool stage is stale."""
    timeout = ORCH_ACTIONABLE_STAGE_TIMEOUT if timeout_sec is None else float(timeout_sec)
    if timeout <= 0 and timeout_sec is None:
        return None
    try:
        from evolution_core import read_pipeline_checkpoint
        checkpoint = read_pipeline_checkpoint()
    except Exception:
        return None
    if not checkpoint:
        return None
    stage = checkpoint.get("stage")
    from pipeline_state import pipeline_runtime_activity_ts

    last_ts = max(
        float(checkpoint.get("last_stage_change_ts") or 0.0),
        float(checkpoint.get("last_update_ts") or 0.0),
        pipeline_runtime_activity_ts(checkpoint),
    )
    if last_ts <= 0:
        return None
    elapsed = time.time() - last_ts
    if elapsed < timeout:
        return None
    route = _resolve_recovery_route(checkpoint)
    if not route:
        return None
    return {
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": stage,
        "elapsed_sec": round(elapsed, 1),
        "timeout_sec": timeout,
        "next_tool": route.get("next_tool"),
        "directive": route.get("directive"),
        "checkpoint_actionable_identity": _checkpoint_actionable_identity(
            checkpoint
        ),
        "stream_owned_route_identity": (
            _checkpoint_stream_owned_route_identity(
                checkpoint,
                resolved_route=route,
            )
        ),
    }


def _checkpoint_actionable_identity(checkpoint):
    """Return the persisted identity that fences a provider-cycle handoff.

    A checkpoint already actionable when a fresh provider session starts is
    the work that session must execute.  Only a different revision/stage
    produced after the session began authorizes disposing that stream.
    """

    if not isinstance(checkpoint, dict):
        return None
    return (
        checkpoint.get("workflow_run_id"),
        checkpoint.get("checkpoint_revision"),
        checkpoint.get("stage"),
        checkpoint.get("next_v"),
        checkpoint.get("source_v"),
    )


def _checkpoint_stream_owned_route_identity(checkpoint, *, resolved_route=None):
    """Return the semantic route owned by an in-flight provider tool call.

    Long-running tools may publish runtime heartbeats or same-stage retry
    metadata while they still own the call.  Those updates must not make the
    orchestrator's idle poller treat the stage that the fresh stream was
    launched to execute as abandoned.  The route tool and intent are included
    because authoritative recovery policy can expose different tools for the
    same persisted stage as its bound gate metadata advances.
    """

    if not isinstance(checkpoint, dict):
        return None
    route = resolved_route
    if route is None:
        route = _resolve_recovery_route(checkpoint)
    if not isinstance(route, dict):
        return None
    policy = route.get("route") or {}
    if not isinstance(policy, dict):
        policy = {}
    return (
        checkpoint.get("workflow_run_id"),
        checkpoint.get("stage"),
        checkpoint.get("next_v"),
        checkpoint.get("source_v"),
        route.get("next_tool"),
        policy.get("intent"),
    )


def _detect_actionable_stage_handoff(
    *,
    baseline_checkpoint_identity=None,
    baseline_checkpoint=None,
    terminal_tool_result=None,
):
    """Return route data when an MCP gate has just produced a deterministic step."""
    stall = _detect_actionable_stage_stall(timeout_sec=0)
    if stall and (
        baseline_checkpoint_identity is None
        or stall.get("checkpoint_actionable_identity")
        != baseline_checkpoint_identity
    ):
        return stall
    observation = _pipeline_checkpoint_observation()
    checkpoint = observation.get("checkpoint")
    checkpoint_error = observation.get("error")
    if checkpoint_error:
        return {
            "next_v": (
                baseline_checkpoint.get("next_v")
                if isinstance(baseline_checkpoint, dict)
                else None
            ),
            "source_v": (
                baseline_checkpoint.get("source_v")
                if isinstance(baseline_checkpoint, dict)
                else None
            ),
            "stage": "checkpoint_recovery_blocked",
            "next_tool": None,
            "recovery_blocked": True,
            "issues": [str(checkpoint_error)],
            "directive": (
                "End the current provider stream. The checkpoint path is "
                "present but unreadable/invalid, or checkpoint authority could "
                "not be read. Outer recovery must fail closed and must not "
                "prepare another generation."
            ),
        }
    if (
        isinstance(checkpoint, dict)
        and checkpoint.get("stage") == "official_bootstrap_required"
    ):
        return {
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "stage": "official_bootstrap_required",
            "next_tool": None,
            "operator_action_required": True,
            "directive": (
                "Stop automatic evolution and wait for the explicit operator "
                "bootstrap-first-strict suite. Automation must not authorize or consume it."
            ),
        }
    if not checkpoint:
        try:
            from post_publication_handoff import pending_handoff_route

            handoff = pending_handoff_route()
        except Exception as exc:
            handoff = {
                "status": "blocked",
                "issues": [f"handoff_discovery_failed:{type(exc).__name__}"],
            }
        if handoff.get("status") == "pending":
            return {
                "next_v": handoff.get("version"),
                "source_v": handoff.get("source_v"),
                "stage": "post_publication_handoff",
                "next_tool": "run_archivist",
                "directive": (
                    "End the current provider stream and resume the exact "
                    "durable Archivist handoff."
                ),
            }
        if handoff.get("status") == "blocked":
            return {
                "next_v": None,
                "source_v": None,
                "stage": "post_publication_handoff_blocked",
                "next_tool": None,
                "recovery_blocked": True,
                "issues": list(handoff.get("issues") or []),
                "directive": (
                    "End the current provider stream. Checkpoint-free recovery "
                    "is blocked by post-publication handoff diagnostics; the "
                    "outer recovery loop must surface them and must not prepare."
                ),
            }
        if handoff.get("status") == "none" and baseline_checkpoint_identity is not None:
            (
                workflow_run_id,
                checkpoint_revision,
                previous_stage,
                next_v,
                source_v,
            ) = baseline_checkpoint_identity
            if not isinstance(baseline_checkpoint, dict):
                return {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": "generation_terminal_proof_blocked",
                    "next_tool": None,
                    "recovery_blocked": True,
                    "issues": ["terminal_baseline_checkpoint_missing"],
                    "directive": (
                        "End the current provider stream. A checkpoint vanished "
                        "without the full stream-owned baseline needed to prove "
                        "canonical termination."
                    ),
                }
            try:
                from tool_bot_management import validate_completed_abandon_handoff

                terminal_proof = validate_completed_abandon_handoff(
                    baseline_checkpoint,
                    terminal_tool_result,
                )
            except Exception as exc:
                issue = str(exc).strip() or type(exc).__name__
                return {
                    "workflow_run_id": workflow_run_id,
                    "checkpoint_revision": checkpoint_revision,
                    "previous_stage": previous_stage,
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": "generation_terminal_proof_blocked",
                    "next_tool": None,
                    "recovery_blocked": True,
                    "issues": [f"canonical_abandon_proof_invalid:{issue[:240]}"],
                    "directive": (
                        "End the current provider stream. The checkpoint "
                        "disappeared without an exact current-head abandon "
                        "transaction, ledger, finalize receipt, and matching "
                        "tool-result proof. Outer recovery must not prepare."
                    ),
                }
            return {
                "workflow_run_id": workflow_run_id,
                "checkpoint_revision": terminal_proof[
                    "checkpoint_identity"
                ]["checkpoint_revision"],
                "baseline_checkpoint_revision": checkpoint_revision,
                "previous_stage": previous_stage,
                "terminal_checkpoint_stage": terminal_proof[
                    "checkpoint_identity"
                ]["stage"],
                "next_v": next_v,
                "source_v": source_v,
                "stage": "generation_terminal",
                "next_tool": "prepare_generation",
                "scheduler_handoff_required": True,
                "terminal_proof": terminal_proof,
                "directive": (
                    "End the current provider stream after canonical generation "
                    "termination. The outer scheduler, not an MCP tool, owns "
                    "the next prepare_generation call."
                ),
            }
    return None


async def _await_next_stream_message(
    stream_iter,
    last_message_at=None,
    *,
    stream_started_at=None,
    baseline_owned_route_identity=None,
):
    """Wait for the next orchestrator stream message with checkpoint-aware polling.

    D (2026-07-09): also enforce a generic mid-stream stall ceiling
    (ORCH_STREAM_STALL_TIMEOUT) on main-stream silence. The ceiling is extended
    only by current-generation MCP tool/sub-role progress, which prevents a
    healthy long tool call from being mistaken for a dead SDK stream while still
    catching truly silent cycles before CYCLE_TIMEOUT (5400s).
    """
    pending = asyncio.create_task(stream_iter.__anext__())
    pending_cleanup_owned = False
    _silence_origin = last_message_at if last_message_at is not None else (stream_started_at or time.time())
    _last_progress_marker = None
    try:
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=max(0.1, ORCH_STREAM_POLL_INTERVAL),
                )
            except asyncio.TimeoutError:
                # D: generic stall ceiling — fires with or without a checkpoint.
                if ORCH_STREAM_STALL_TIMEOUT > 0:
                    progress = _latest_orchestrator_external_progress(_silence_origin)
                    if progress:
                        progress_ts = min(time.time(), _coerce_event_ts(progress.get("ts")))
                        if progress_ts > _silence_origin:
                            _silence_origin = progress_ts
                            marker = (
                                progress.get("source"),
                                progress.get("event_type"),
                                progress.get("ts"),
                            )
                            if marker != _last_progress_marker:
                                _last_progress_marker = marker
                                try:
                                    log_system_event(
                                        "pipeline.orchestrator_stream_external_progress",
                                        "info",
                                        "Orchestrator main stream is silent, but current-generation tool progress is visible",
                                        {
                                            "progress_source": progress.get("source"),
                                            "progress_event_type": progress.get("event_type"),
                                            "progress_ts": round(progress_ts, 3),
                                            "next_v": progress.get("next_v"),
                                            "stage": progress.get("stage"),
                                            "role": progress.get("role"),
                                            "log_file": progress.get("log_file"),
                                            "stall_timeout": ORCH_STREAM_STALL_TIMEOUT,
                                        },
                                    )
                                except Exception:
                                    pass
                            continue
                    silent_for = time.time() - _silence_origin
                    if silent_for >= ORCH_STREAM_STALL_TIMEOUT:
                        msg = (
                            f"Orchestrator main-agent stream stalled: no stream "
                            f"message for {silent_for:.0f}s (ceiling "
                            f"{ORCH_STREAM_STALL_TIMEOUT:.0f}s). Treating as "
                            f"infrastructure stall; cycle will retry."
                        )
                        try:
                            log_system_event(
                                "pipeline.orchestrator_stream_stall_timeout",
                                "warn",
                                msg,
                                {
                                    "silent_for_sec": round(silent_for, 1),
                                    "stall_timeout": ORCH_STREAM_STALL_TIMEOUT,
                                },
                            )
                        except Exception:
                            pass
                        pending_cleanup_owned = not await cancel_provider_stream_task_bounded(
                            pending,
                            "orchestrator_stream_stall_cancellation_unconfirmed",
                        )
                        raise _OrchStreamStallTimeout(msg)
                stall = _detect_actionable_stage_stall()
                if (
                    stall
                    and baseline_owned_route_identity is not None
                    and ORCH_STREAM_STALL_TIMEOUT > 0
                ):
                    if (
                        stall.get("stream_owned_route_identity")
                        == baseline_owned_route_identity
                    ):
                        # The fresh stream still owns the exact semantic route
                        # it was started to execute. Its nested role may
                        # legitimately run longer than the stale-actionable
                        # ceiling; generic stream/tool progress supervision
                        # remains in force.
                        stall = None
                if not stall:
                    continue
                next_v = stall.get("next_v")
                stage = stall.get("stage")
                next_tool = stall.get("next_tool") or "unknown"
                msg = (
                    f"Orchestrator stream idle while v{next_v} is at actionable "
                    f"stage '{stage}' for {stall.get('elapsed_sec')}s; "
                    f"fresh cycle should call {next_tool}."
                )
                try:
                    log_system_event(
                        "pipeline.actionable_stage_timeout",
                        "warn",
                        msg,
                        stall,
                    )
                except Exception:
                    pass
                pending_cleanup_owned = not await cancel_provider_stream_task_bounded(
                    pending,
                    "orchestrator_actionable_stall_cancellation_unconfirmed",
                )
                raise _OrchActionableStageTimeout(msg)
    except BaseException:
        if not pending.done() and not pending_cleanup_owned:
            await cancel_provider_stream_task_bounded(
                pending,
                "orchestrator_stream_parent_cancellation_unconfirmed",
            )
        raise


def _orchestrator_cycle_cancel_grace():
    try:
        return max(
            0.0,
            min(
                30.0,
                float(os.environ.get("POK_ORCH_CYCLE_CANCEL_GRACE", "1")),
            ),
        )
    except (TypeError, ValueError):
        return 1.0


def _orchestrator_task_error(task):
    try:
        task.result()
    except BaseException as exc:
        return exc
    return None


async def _cancel_orchestrator_stream_task_bounded(
    stream_task,
    *,
    attempt_ref,
    gen_ref,
    reason,
    log_file_path,
):
    """Cancel one cycle task and close only its proven provider transport."""

    attempt = attempt_ref[0] if attempt_ref else None
    # Revoke native-match liveness before task cancellation/transport cleanup.
    # A detached tool coroutine can retain its ContextVar, so merely cancelling
    # the provider task is not enough to stop an old match from extending a
    # later SDK dispatch.
    if isinstance(attempt, dict):
        try:
            from pipeline_state import revoke_native_match_dispatch_nonce

            revoke_native_match_dispatch_nonce(str(attempt.get("attempt_id") or ""))
        except Exception:
            pass
    if stream_task.done():
        error = _orchestrator_task_error(stream_task)
        return error if isinstance(error, LLMProviderCleanupError) else None

    stream_task.cancel()
    grace = _orchestrator_cycle_cancel_grace()
    if grace > 0:
        await asyncio.wait({stream_task}, timeout=grace)
    if stream_task.done():
        error = _orchestrator_task_error(stream_task)
        return error if isinstance(error, LLMProviderCleanupError) else None

    query_gen = gen_ref[0] if gen_ref else None
    cleanup_error = None
    if isinstance(attempt, dict):
        # Do not add ``stream_task`` yet: it may currently be awaiting the
        # shared cleanup task in its own finally block. Tracking it before that
        # cleanup completes would create a self-dependency. Mark the attempt,
        # run/await the single shared cleanup, then retain the owner task only
        # if it still refuses to exit.
        mark_owned_provider_attempt_unresolved(attempt, reason)
        if query_gen is not None:
            try:
                with owned_provider_attempt_scope(attempt):
                    await cleanup_owned_provider_attempt(
                        query_gen,
                        attempt,
                        "ORCHESTRATOR",
                        log_file_path,
                    )
            except LLMProviderCleanupError as exc:
                cleanup_error = exc
            except BaseException as exc:
                cleanup_error = LLMProviderCleanupError(
                    "orchestrator owned provider cleanup failed: "
                    f"{type(exc).__name__}: {str(exc)[:300]}",
                    provider_exit_confirmed=False,
                    attempt_id=attempt.get("attempt_id"),
                )
                mark_owned_provider_attempt_unresolved(
                    attempt,
                    f"orchestrator_cleanup_failed:{type(exc).__name__}",
                )
        else:
            cleanup_error = LLMProviderCleanupError(
                "orchestrator provider attempt has no query generator for cleanup",
                provider_exit_confirmed=False,
                attempt_id=attempt.get("attempt_id"),
            )
    else:
        cleanup_error = LLMProviderCleanupError(
            "orchestrator cycle task resisted cancellation before provider ownership was published",
            provider_exit_confirmed=False,
        )

    post_cleanup_grace = max(0.1, grace)
    await asyncio.wait({stream_task}, timeout=post_cleanup_grace)
    if not stream_task.done():
        if isinstance(attempt, dict):
            mark_owned_provider_attempt_unresolved(
                attempt,
                f"{reason}:owner_task_pending",
                stream_task,
            )
            confirmed = owned_provider_attempt_exit_confirmed(attempt)
            cleanup_error = LLMProviderCleanupError(
                "orchestrator stream owner task remained pending after owned transport cleanup",
                provider_exit_confirmed=confirmed,
                attempt_id=attempt.get("attempt_id"),
            )
        else:
            stream_task.add_done_callback(_orchestrator_task_error)
        return cleanup_error

    task_error = _orchestrator_task_error(stream_task)
    if cleanup_error is None and isinstance(task_error, LLMProviderCleanupError):
        cleanup_error = task_error
    if isinstance(attempt, dict):
        owned_provider_attempt_exit_confirmed(attempt)
    return cleanup_error


def _bounded_native_match_extension(
    *,
    stream_started_epoch: float,
    original_deadline_epoch: float,
    provider_dispatch_nonce: str | None,
) -> dict | None:
    """Return one eligible engine-match extension, or ``None`` fail-closed."""

    try:
        from pipeline_state import (
            native_match_dispatch_nonce_is_active,
            read_pipeline_native_match_progress,
            validate_native_match_progress,
        )

        if not native_match_dispatch_nonce_is_active(provider_dispatch_nonce):
            return None
        checkpoint = _read_active_pipeline_checkpoint()
        if not isinstance(checkpoint, dict):
            return None
        progress = read_pipeline_native_match_progress(
            checkpoint,
            now=time.time(),
            max_age=ORCH_NATIVE_MATCH_PROGRESS_MAX_AGE_SEC,
            provider_dispatch_nonce=provider_dispatch_nonce,
        )
        # Revalidate here as well as at sidecar read time.  The bounded
        # extension is a privileged liveness exception and must stay closed
        # even if a caller/test substitutes the read helper's output.
        progress = validate_native_match_progress(
            checkpoint,
            progress,
            now=time.time(),
            provider_dispatch_nonce=provider_dispatch_nonce,
        )
    except Exception:
        return None
    if not isinstance(progress, dict):
        return None
    # The sidecar validator already binds owner->stage, PID/revision, frozen
    # phase budget and this exact provider dispatch nonce.  Timestamp proximity
    # is intentionally not an identity fence: an old tool can begin within a
    # few seconds of a new stream, while an exact owned SDK nonce cannot cross
    # that boundary.
    phase_deadline = float(progress.get("phase_deadline_epoch") or 0.0)
    operation_deadline = float(progress.get("operation_deadline_epoch") or 0.0)
    now = time.time()
    if (
        phase_deadline <= now
        or operation_deadline <= now
        or progress.get("terminal") is not False
        or progress.get("provider_dispatch_nonce") != provider_dispatch_nonce
    ):
        return None
    absolute_cap = float(original_deadline_epoch) + ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC
    deadline = min(
        phase_deadline,
        operation_deadline,
        absolute_cap,
    )
    if deadline <= now:
        return None
    return {
        "deadline_epoch": deadline,
        "cap_epoch": absolute_cap,
        "checkpoint": checkpoint,
        "checkpoint_identity": _checkpoint_actionable_identity(checkpoint),
        "progress": progress,
    }


def _native_match_extension_reproof(previous: dict, fresh: dict | None) -> bool:
    """Prove that a granted extension still belongs to one immutable match."""

    if not isinstance(previous, dict) or not isinstance(fresh, dict):
        return False
    if previous.get("checkpoint_identity") != fresh.get("checkpoint_identity"):
        return False
    if previous.get("cap_epoch") != fresh.get("cap_epoch"):
        return False
    old = previous.get("progress") or {}
    new = fresh.get("progress") or {}
    immutable_fields = (
        "owner_tool",
        "provider_dispatch_nonce",
        "match_identity_digest",
        "timing_plan_digest",
        "hands",
        "effective_timeout_us",
        "operation_started_at_epoch",
        "operation_deadline_epoch",
        "operation_budget_us",
    )
    if any(old.get(field) != new.get(field) for field in immutable_fields):
        return False
    try:
        if int(new.get("event_seq")) < int(old.get("event_seq")):
            return False
    except (TypeError, ValueError):
        return False
    phase_order = {"launching": 0, "engine_running": 1, "finalizing": 2}
    old_phase = str(old.get("liveness_phase") or "")
    new_phase = str(new.get("liveness_phase") or "")
    if old_phase not in phase_order or new_phase not in phase_order:
        return False
    if phase_order[new_phase] < phase_order[old_phase]:
        return False
    old_hand = old.get("hand")
    new_hand = new.get("hand")
    old_hand_order = 0 if old_hand is None else int(old_hand)
    new_hand_order = 0 if new_hand is None else int(new_hand)
    if new_hand_order < old_hand_order:
        return False
    if new_phase == old_phase:
        for field in (
            "phase_started_at_epoch",
            "phase_deadline_epoch",
            "phase_budget_us",
        ):
            if old.get(field) != new.get(field):
                return False
    return True


def _native_match_terminal_handoff_checkpoint_valid(
    extension: dict,
    receipt: dict,
) -> bool:
    """Require the current checkpoint to be the same owner flow or its result."""

    old_checkpoint = extension.get("checkpoint") or {}
    current = _read_active_pipeline_checkpoint()
    if not isinstance(old_checkpoint, dict) or not isinstance(current, dict):
        return False
    old_workflow = str(
        old_checkpoint.get("workflow_run_id")
        or old_checkpoint.get("run_id")
        or ""
    )
    current_workflow = str(
        current.get("workflow_run_id") or current.get("run_id") or ""
    )
    owner = str(receipt.get("owner_tool") or "")
    allowed_stages = {
        "run_quality_gates": {
            "workers_done",
            "quality_failed",
            "quality_passed",
        },
        "run_precommit_eval": {
            "critic_checked",
            "precommit_failed",
            "verified",
            "infra_timed_out",
        },
    }.get(owner, set())
    try:
        old_revision = int(old_checkpoint.get("checkpoint_revision") or 0)
        current_revision = int(current.get("checkpoint_revision") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        old_workflow
        and current_workflow == old_workflow
        and current.get("next_v") == old_checkpoint.get("next_v")
        and current.get("source_v") == old_checkpoint.get("source_v")
        and current_revision >= old_revision
        and str(current.get("stage") or "") in allowed_stages
    )


def _consume_native_match_terminal_handoff(
    extension: dict,
    *,
    observed_at_epoch: float,
) -> dict | None:
    """Consume a runner-return receipt for one immutable granted extension."""

    if not isinstance(extension, dict):
        return None
    checkpoint = extension.get("checkpoint")
    progress = extension.get("progress")
    if not isinstance(checkpoint, dict) or not isinstance(progress, dict):
        return None
    try:
        from pipeline_state import consume_native_match_terminal_handoff

        receipt = consume_native_match_terminal_handoff(
            checkpoint,
            progress,
            now=observed_at_epoch,
        )
    except Exception:
        return None
    if not isinstance(receipt, dict):
        return None
    try:
        created_at = float(receipt.get("created_at_epoch"))
        receipt_expiry = float(receipt.get("expires_at_epoch"))
        cap_epoch = float(extension.get("cap_epoch"))
        extension_deadline = float(extension.get("deadline_epoch"))
        operation_deadline = float(progress.get("operation_deadline_epoch"))
        handoff_deadline = min(
            receipt_expiry,
            cap_epoch,
            extension_deadline,
            operation_deadline,
        )
        previous_seq = int(progress.get("event_seq"))
        last_live_seq = int(receipt.get("last_live_event_seq"))
        terminal_seq = int(receipt.get("terminal_event_seq"))
    except (TypeError, ValueError):
        return None
    if (
        receipt.get("terminal_outcome") != "runner_returned"
        or last_live_seq < previous_seq
        or terminal_seq != last_live_seq + 1
        or created_at > observed_at_epoch + 1.0
        or observed_at_epoch > handoff_deadline
        or not _native_match_terminal_handoff_checkpoint_valid(
            extension,
            receipt,
        )
    ):
        return None
    return {
        "receipt": receipt,
        "deadline_epoch": handoff_deadline,
        "checkpoint_identity": extension.get("checkpoint_identity"),
    }


def _native_match_terminal_handoff_reproof(
    state: dict | None,
    *,
    observed_at_epoch: float,
) -> bool:
    """Validate a consumed handoff without extending its fixed expiry."""

    if not isinstance(state, dict):
        return False
    receipt = state.get("receipt") or {}
    try:
        deadline = float(state.get("deadline_epoch"))
    except (TypeError, ValueError):
        return False
    extension = {
        "checkpoint": {
            "workflow_run_id": receipt.get("workflow_run_id"),
            "checkpoint_revision": receipt.get("checkpoint_revision"),
            "stage": receipt.get("stage"),
            "next_v": receipt.get("next_v"),
            "source_v": receipt.get("source_v"),
        },
    }
    return bool(
        receipt.get("terminal_outcome") == "runner_returned"
        and observed_at_epoch <= deadline
        and _native_match_terminal_handoff_checkpoint_valid(extension, receipt)
    )


async def _await_orchestrator_stream_response_bounded(
    stream_coro,
    *,
    timeout,
    attempt_ref,
    gen_ref,
    log_file_path,
):
    """Bound one provider stream, with at most one frozen native-match grace."""

    stream_task = asyncio.create_task(stream_coro)
    stream_started_epoch = time.time()
    original_deadline_epoch = stream_started_epoch + float(timeout)
    wait_deadline_monotonic = time.monotonic() + float(timeout)
    native_extension_granted = False
    native_extension_state = None
    terminal_handoff_state = None
    provider_dispatch_nonce = None
    dispatch_revoked = False

    def revoke_dispatch():
        nonlocal dispatch_revoked
        if dispatch_revoked:
            return
        attempt = attempt_ref[0] if attempt_ref else None
        nonce = provider_dispatch_nonce
        if not nonce and isinstance(attempt, dict):
            nonce = str(attempt.get("attempt_id") or "")
        if not nonce:
            return
        try:
            from pipeline_state import revoke_native_match_dispatch_nonce

            revoke_native_match_dispatch_nonce(nonce)
            dispatch_revoked = True
        except Exception:
            pass

    try:
        try:
            while True:
                remaining = max(0.0, wait_deadline_monotonic - time.monotonic())
                poll_timeout = remaining
                if native_extension_granted and terminal_handoff_state is None:
                    poll_timeout = min(
                        remaining,
                        max(0.01, ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC),
                    )
                done, _pending = await asyncio.wait(
                    {stream_task},
                    timeout=poll_timeout,
                )
                observed_at = time.time()
                if stream_task in done:
                    if not native_extension_granted:
                        return stream_task.result()
                    if terminal_handoff_state is not None:
                        if _native_match_terminal_handoff_reproof(
                            terminal_handoff_state,
                            observed_at_epoch=observed_at,
                        ):
                            return stream_task.result()
                    else:
                        # Completion is accepted only with the exact last live
                        # proof or the runner's one-shot terminal replacement.
                        fresh = _bounded_native_match_extension(
                            stream_started_epoch=stream_started_epoch,
                            original_deadline_epoch=original_deadline_epoch,
                            provider_dispatch_nonce=provider_dispatch_nonce,
                        )
                        if _native_match_extension_reproof(
                            native_extension_state,
                            fresh,
                        ):
                            return stream_task.result()
                        terminal_handoff_state = (
                            _consume_native_match_terminal_handoff(
                                native_extension_state,
                                observed_at_epoch=observed_at,
                            )
                        )
                        if terminal_handoff_state is not None:
                            # No later match under this dispatch may borrow the
                            # consumed receipt.  The outer in-memory state now
                            # owns only its fixed, non-renewable handoff window.
                            revoke_dispatch()
                            if _native_match_terminal_handoff_reproof(
                                terminal_handoff_state,
                                observed_at_epoch=observed_at,
                            ):
                                return stream_task.result()
                    revoke_dispatch()
                    log_system_event(
                        "pipeline.orchestrator_native_match_extension_revoked",
                        "error",
                        "A completed provider stream lost its final exact native-match proof.",
                        {
                            "provider_dispatch_nonce": provider_dispatch_nonce,
                            "match_identity_digest": (
                                (native_extension_state or {}).get("progress") or {}
                            ).get("match_identity_digest"),
                        },
                    )
                    break
                if not native_extension_granted:
                    attempt = attempt_ref[0] if attempt_ref else None
                    provider_dispatch_nonce = (
                        str(attempt.get("attempt_id") or "")
                        if isinstance(attempt, dict)
                        else None
                    )
                    extension = _bounded_native_match_extension(
                        stream_started_epoch=stream_started_epoch,
                        original_deadline_epoch=original_deadline_epoch,
                        provider_dispatch_nonce=provider_dispatch_nonce,
                    )
                    if extension is not None:
                        native_extension_granted = True
                        native_extension_state = extension
                        extended_deadline = float(extension["deadline_epoch"])
                        wait_deadline_monotonic = (
                            time.monotonic()
                            + max(0.0, extended_deadline - time.time())
                        )
                        progress = extension["progress"]
                        log_system_event(
                            "pipeline.orchestrator_native_match_extension_granted",
                            "warn",
                            "Granted one bounded provider-cycle extension for a live "
                            "checkpoint-bound native TCP match.",
                            {
                                "owner_tool": progress.get("owner_tool"),
                                "stage": (extension["checkpoint"] or {}).get("stage"),
                                "match_identity_digest": progress.get(
                                    "match_identity_digest"
                                ),
                                "timing_plan_digest": progress.get(
                                    "timing_plan_digest"
                                ),
                                "event_seq": progress.get("event_seq"),
                                "phase_deadline_epoch": progress.get(
                                    "phase_deadline_epoch"
                                ),
                                "operation_deadline_epoch": progress.get(
                                    "operation_deadline_epoch"
                                ),
                                "extension_deadline_epoch": extended_deadline,
                                "absolute_cap_epoch": extension.get("cap_epoch"),
                            },
                        )
                        continue
                elif terminal_handoff_state is None:
                    fresh = _bounded_native_match_extension(
                        stream_started_epoch=stream_started_epoch,
                        original_deadline_epoch=original_deadline_epoch,
                        provider_dispatch_nonce=provider_dispatch_nonce,
                    )
                    if _native_match_extension_reproof(
                        native_extension_state,
                        fresh,
                    ):
                        native_extension_state = fresh
                        extended_deadline = float(fresh["deadline_epoch"])
                        wait_deadline_monotonic = (
                            time.monotonic()
                            + max(0.0, extended_deadline - time.time())
                        )
                        continue
                    terminal_handoff_state = _consume_native_match_terminal_handoff(
                        native_extension_state,
                        observed_at_epoch=observed_at,
                    )
                    if terminal_handoff_state is not None:
                        revoke_dispatch()
                        handoff_deadline = float(
                            terminal_handoff_state["deadline_epoch"]
                        )
                        wait_deadline_monotonic = (
                            time.monotonic()
                            + max(0.0, handoff_deadline - time.time())
                        )
                        log_system_event(
                            "pipeline.orchestrator_native_match_terminal_handoff",
                            "info",
                            "Consumed one exact runner terminal receipt; awaiting only "
                            "the fixed provider-result handoff window.",
                            {
                                "provider_dispatch_nonce": provider_dispatch_nonce,
                                "match_identity_digest": (
                                    terminal_handoff_state["receipt"].get(
                                        "match_identity_digest"
                                    )
                                ),
                                "terminal_event_seq": (
                                    terminal_handoff_state["receipt"].get(
                                        "terminal_event_seq"
                                    )
                                ),
                                "handoff_deadline_epoch": handoff_deadline,
                            },
                        )
                        continue
                    revoke_dispatch()
                    log_system_event(
                        "pipeline.orchestrator_native_match_extension_revoked",
                        "error",
                        "A granted native-match extension lost its exact live proof.",
                        {
                            "provider_dispatch_nonce": provider_dispatch_nonce,
                            "match_identity_digest": (
                                (native_extension_state or {}).get("progress") or {}
                            ).get("match_identity_digest"),
                        },
                    )
                    break
                if native_extension_granted:
                    log_system_event(
                        "pipeline.orchestrator_native_match_extension_exhausted",
                        "error",
                        "The one bounded native-match provider extension expired.",
                        {"timeout_sec": float(timeout)},
                    )
                break
        except BaseException:
            await _cancel_orchestrator_stream_task_bounded(
                stream_task,
                attempt_ref=attempt_ref,
                gen_ref=gen_ref,
                reason="orchestrator_cycle_parent_cancellation_unconfirmed",
                log_file_path=log_file_path,
            )
            raise
        cleanup_error = await _cancel_orchestrator_stream_task_bounded(
            stream_task,
            attempt_ref=attempt_ref,
            gen_ref=gen_ref,
            reason="orchestrator_cycle_timeout_cancellation_unconfirmed",
            log_file_path=log_file_path,
        )
        timeout_error = asyncio.TimeoutError(
            f"orchestrator SDK stream exceeded cycle timeout {float(timeout):.1f}s"
        )
        if cleanup_error is not None:
            raise timeout_error from cleanup_error
        raise timeout_error
    finally:
        revoke_dispatch()


async def _run_one_cycle(
    ui,
    log_file,
    one_gen=False,
    dry_run=False,
    max_turns=None,
    gen_ctx=None,
    shutdown_mgr=None,
    _cost_policy: GenerationCostPolicy | None = None,
):
    """Run one Orchestrator cycle (one LLM agent session). Returns total cost."""
    set_cycle_start_time(time.time())
    context = _build_context(one_gen=one_gen, dry_run=dry_run, gen_ctx=gen_ctx)
    from llm_query import render_llm_prompt

    rendered_prompt = render_llm_prompt(
        "Orchestrator",
        producer=_render_orchestrator_provider_prompt,
        renderer_inputs={"context": context, "dry_run": bool(dry_run)},
        mcp_servers={"evolution": evolution_server},
    )

    # Orchestrator owns a streaming MCP session and therefore cannot delegate
    # transport to ``run_claude_query``.  It still consumes the same fail-closed
    # role registry and receives the same final provider prompt boundary as all
    # sub-agent roles.  The only provider-visible capability is the typed
    # evolution MCP server; built-in filesystem/shell tools remain absent.
    prompt, _orchestrator_role_contract = bind_llm_role_provider_prompt(
        rendered_prompt,
        "Orchestrator",
        tools=[],
        provider_path="orchestrator_sdk",
        mcp_servers={"evolution": evolution_server},
        model="sonnet",
    )

    # Pipeline recovery is checkpoint-driven.  Provider session IDs are opaque
    # server-side history capabilities and are never loaded into SDK ``resume``.
    checkpoint_observation = _pipeline_checkpoint_observation()
    if checkpoint_observation.get("error"):
        issue = str(checkpoint_observation["error"])
        msg = (
            "Refusing to open an Orchestrator provider stream because "
            f"checkpoint authority is unreadable or invalid: {issue}."
        )
        if ui:
            ui.log_history(msg, "error")
            ui.set_status("Stopped: checkpoint authority invalid", is_working=False)
        log.error(msg)
        try:
            log_system_event(
                "orchestrator.checkpoint_authority_blocked",
                "error",
                msg,
                {
                    "issue": issue,
                    "checkpoint_path_exists": checkpoint_observation.get(
                        "path_exists"
                    ),
                },
            )
        except Exception:
            pass
        return ORCH_RECOVERY_BLOCKED_COST
    checkpoint = checkpoint_observation.get("checkpoint")
    baseline_checkpoint = (
        json.loads(json.dumps(checkpoint))
        if isinstance(checkpoint, dict)
        else None
    )
    baseline_checkpoint_identity = _checkpoint_actionable_identity(checkpoint)
    baseline_owned_route_identity = _checkpoint_stream_owned_route_identity(
        checkpoint
    )
    _bind_generation_cost_runtime(
        checkpoint,
        gen_ctx=gen_ctx,
        ui=ui,
        policy=_cost_policy,
    )
    try:
        _check_generation_cost_policy(ui)
    except OperatorGenerationCostLimitExceeded:
        _clear_orchestrator_session(reason="operator_generation_cost_limit")
        if ui:
            ui.set_status("Stopped: operator generation cost limit", is_working=False)
        return ORCH_OPERATOR_COST_LIMIT_COST
    _load_orchestrator_session()  # removes any pre-policy legacy sidecar

    from evolution_core import _BLOCKED_MCP_TOOLS
    # P1 (2026-06-29): merge PreCompact hook (state preservation) with the
    # bot_dir_guard PreToolUse hook (blocks LLM from hand-editing bot code via
    # Bash/Edit/Write, which bypassed the H6 circuit breaker in v218).
    _hooks = {**_make_precompact_hook(), **_make_bot_dir_guard_hook()}
    options = ClaudeAgentOptions(
        model="sonnet",
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        mcp_servers={"evolution": evolution_server},
        strict_mcp_config=True,
        # The main planner needs only the typed evolution MCP server. Removing
        # built-ins closes dynamic Python/shell import routes to operator-owned
        # pause, official-bootstrap, and strict-authority state.
        tools=[],
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        hooks=_hooks,
        max_turns=max_turns,
        thinking={"type": "adaptive"},  # let Claude decide thinking depth (was disabled to dodge an old SDK signature bug; adaptive is now the documented default)
    )

    total_cost = 0.0
    cycle_completed = False
    auth_error = False
    cycle_failed = False  # P1: generic-exception path must not return partial cost (fake success)
    infra_error = False  # P2: SDK signature/timeout/connection — distinct from real auth (-0.5 vs -1.0)
    shutdown_cancelled = False
    stream_invocation_count = 0
    # Snapshot sub-agent costs at start to compute delta on return.
    # ui.gen_cost_total tracks ALL sub-agent costs (Master, Workers, etc.)
    # via llm_query.py. The orchestrator's own ResultMessage is projected at the
    # same durable-record boundary, before a hard-limit exception can unwind.
    _cost_at_start = ui.gen_cost_total if ui else 0.0

    with open(log_file, "a") as lf:
        lf.write(f"\n{'='*60}\n[ORCHESTRATOR CYCLE] {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")
        lf.write(f"[PROMPT]\n{prompt}\n\n[OUTPUT]\n")

        async def _stream_response(opts, max_retries=3):
            """Run a single streaming query. Returns (full_text, cost, cycle_ok, gen, auth_error)."""
            global _orchestrator_provider_stream_active
            nonlocal stream_invocation_count
            stream_invocation_count += 1
            stream_invocation_id = stream_invocation_count
            texts = []
            cost = 0.0
            ok = False
            gen = None
            auth_err = False
            _tool_call_counts = {}
            _pending_tool_uses = {}
            _seen_tool_use_ids = set()
            _ignored_user_tool_use_ids = set()
            _terminal_tool_result_for_batch = None
            availability_trace = LLMAvailabilityTrace()

            def _canonical_json_bytes(value):
                try:
                    return json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                except (TypeError, ValueError):
                    return None

            def _current_verified_terminal_cache():
                """Read the active attempt's guarded-handler-only cache."""

                try:
                    from llm_query import current_provider_verified_terminal_abandon

                    return current_provider_verified_terminal_abandon()
                except Exception:
                    return None

            def _validated_cached_terminal_for_owner(owner, tool_use_id):
                """Reprove one cache entry before it can replace an SDK result."""

                record = _current_verified_terminal_cache()
                if record is None:
                    return None
                if not isinstance(record, dict):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_shape_invalid"
                    )
                if owner not in _TERMINAL_ABANDON_RESULT_OWNER_TOOLS:
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_owner_not_terminal:"
                        f"{owner or 'unknown'}"
                    )
                if str(record.get("owner_tool") or "") != str(owner or ""):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_owner_mismatch:"
                        f"{owner or 'unknown'}"
                    )
                if str(record.get("tool_use_id") or "") != str(tool_use_id or ""):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_tool_use_id_mismatch"
                    )
                if not isinstance(record.get("arguments"), str):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_arguments_invalid"
                    )
                cached_result = _completed_abandon_tool_result(
                    record.get("terminal_result")
                )
                if cached_result is None or not isinstance(
                    baseline_checkpoint, dict
                ):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_material_invalid"
                    )
                try:
                    from tool_bot_management import validate_completed_abandon_handoff

                    proof = validate_completed_abandon_handoff(
                        baseline_checkpoint,
                        cached_result,
                    )
                except Exception as exc:
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_proof_invalid:"
                        f"{str(exc)[:160]}"
                    )
                if _canonical_json_bytes(proof) != _canonical_json_bytes(
                    record.get("terminal_proof")
                ):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_proof_mismatch"
                    )
                return cached_result

            def _settle_missing_terminal_result_from_cache():
                """Settle exactly one pending ToolUse only from a proved cache."""

                if not _pending_tool_uses:
                    return None
                record = _current_verified_terminal_cache()
                if record is None:
                    return None
                if len(_pending_tool_uses) != 1:
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_pending_tool_uses_ambiguous"
                    )
                tool_use_id, pending_name = next(iter(_pending_tool_uses.items()))
                owner = _normalized_provider_tool_name(pending_name)
                cached_result = _validated_cached_terminal_for_owner(
                    owner,
                    tool_use_id,
                )
                if cached_result is None:
                    return None
                _pending_tool_uses.pop(tool_use_id, None)
                return cached_result

            def _register_pending_tool_use(block, *, source):
                """Bind either SDK message placement of ToolUseBlock identically."""

                nonlocal _terminal_tool_result_for_batch
                tool_use_id = str(getattr(block, "id", "") or "")
                if not tool_use_id or tool_use_id in _seen_tool_use_ids:
                    _raise_stream_result_binding_blocked(
                        "assistant_tool_use_id_missing_or_duplicate"
                    )
                _seen_tool_use_ids.add(tool_use_id)
                raw_name = str(getattr(block, "name", "") or "")
                if not raw_name.startswith("mcp__evolution__"):
                    if source == "user":
                        # SDK may surface non-MCP/User-side tool annotations in
                        # a UserMessage.  They have no Evolution dispatch
                        # authority and must not create a pending gate lease.
                        _ignored_user_tool_use_ids.add(tool_use_id)
                        return False
                    _raise_stream_result_binding_blocked(
                        "assistant_tool_use_not_evolution_mcp"
                    )
                if not _pending_tool_uses:
                    _terminal_tool_result_for_batch = None
                _pending_tool_uses[tool_use_id] = raw_name
                try:
                    from llm_query import register_current_provider_evolution_tool_use

                    register_current_provider_evolution_tool_use(
                        tool_use_id,
                        raw_name,
                        getattr(block, "input", None),
                    )
                except Exception:
                    # Local binding remains sufficient for the normal SDK
                    # result path.  Only the transport-loss fallback depends
                    # on the stricter attempt-scoped registration.
                    pass
                if ui:
                    ui.log_history(
                        f"[Orchestrator] Calling tool: {raw_name}", "info"
                    )
                    ui.log_io(
                        f"\n[tool: {raw_name}]", "tool", "Orchestrator"
                    )
                    ui.emit_tool_call(raw_name, block.input, "Orchestrator")
                else:
                    log.info("Calling tool: %s", raw_name)
                args_str = json.dumps(
                    block.input, ensure_ascii=False, indent=2
                )[:2000]
                lf.write(f"\n[tool: {raw_name}]\n[args] {args_str}\n")
                tool_name = (
                    raw_name.split("__")[-1]
                    if "__" in raw_name
                    else raw_name
                )
                _tool_call_counts[tool_name] = (
                    _tool_call_counts.get(tool_name, 0) + 1
                )
                threshold = (
                    _REDUNDANT_NOISY_THRESHOLD
                    if tool_name in _NOISY_TOOLS
                    else _REDUNDANT_STRICT_THRESHOLD
                )
                if _tool_call_counts[tool_name] != threshold:
                    return
                allowed_repeat = _classify_allowed_repeated_pipeline_tool(
                    tool_name, block.input
                )
                if allowed_repeat:
                    log.info(
                        "Tool '%s' called %d times on a corrective route: %s",
                        tool_name,
                        _tool_call_counts[tool_name],
                        allowed_repeat.get("reason"),
                    )
                    try:
                        log_system_event(
                            "pipeline.repeated_tool_call_allowed",
                            "info",
                            f"Orchestrator called {tool_name} "
                            f"{_tool_call_counts[tool_name]}x on corrective route "
                            f"{allowed_repeat.get('reason')}",
                            {
                                "tool": tool_name,
                                "count": _tool_call_counts[tool_name],
                                "threshold": threshold,
                                **allowed_repeat,
                            },
                        )
                    except Exception:
                        pass
                    return
                log.warning(
                    "Tool '%s' called %d times (possible redundant call)",
                    tool_name,
                    _tool_call_counts[tool_name],
                )
                try:
                    log_system_event(
                        "pipeline.redundant_tool_call",
                        "warn",
                        f"Orchestrator called {tool_name} "
                        f"{_tool_call_counts[tool_name]}x in one cycle",
                        {
                            "tool": tool_name,
                            "count": _tool_call_counts[tool_name],
                            "threshold": threshold,
                        },
                    )
                except Exception:
                    pass

                return True

            def _settle_registered_evolution_tool_use(tool_use_id):
                try:
                    from llm_query import settle_current_provider_evolution_tool_use

                    settle_current_provider_evolution_tool_use(tool_use_id)
                except Exception:
                    pass

            def _raise_actionable_handoff_if_ready(terminal_tool_result=None):
                handoff = _detect_actionable_stage_handoff(
                    baseline_checkpoint_identity=baseline_checkpoint_identity,
                    baseline_checkpoint=baseline_checkpoint,
                    terminal_tool_result=terminal_tool_result,
                )
                if not handoff:
                    return
                next_v = handoff.get("next_v")
                stage = handoff.get("stage")
                if handoff.get("recovery_blocked"):
                    issues = ", ".join(map(str, handoff.get("issues") or ()))
                    msg = (
                        "Checkpoint-free recovery is blocked by durable handoff "
                        f"diagnostics ({issues or 'unknown'}); ending the current "
                        "Orchestrator stream so outer recovery can fail closed."
                    )
                elif handoff.get("operator_action_required"):
                    msg = (
                        f"Checkpoint parked at '{stage}' for v{next_v}; ending the "
                        "Orchestrator stream and stopping automatic recovery until "
                        "the explicit operator bootstrap succeeds."
                    )
                elif handoff.get("scheduler_handoff_required"):
                    msg = (
                        f"Generation workflow for v{next_v} reached its canonical "
                        "terminal boundary; ending the current Orchestrator stream "
                        "so the outer scheduler can call prepare_generation in a "
                        "fresh cycle."
                    )
                else:
                    next_tool = handoff.get("next_tool") or "unknown"
                    msg = (
                        f"Checkpoint reached actionable stage '{stage}' for v{next_v}; "
                        f"handing off current Orchestrator stream so recovery can call "
                        f"{next_tool} deterministically."
                    )
                try:
                    log_system_event(
                        "pipeline.actionable_stage_handoff",
                        "info",
                        msg,
                        handoff,
                    )
                except Exception:
                    pass
                raise _OrchActionableStageHandoff(msg, handoff)

            def _raise_stream_result_binding_blocked(issue):
                handoff = {
                    "next_v": (
                        baseline_checkpoint.get("next_v")
                        if isinstance(baseline_checkpoint, dict)
                        else None
                    ),
                    "source_v": (
                        baseline_checkpoint.get("source_v")
                        if isinstance(baseline_checkpoint, dict)
                        else None
                    ),
                    "stage": "provider_tool_result_binding_blocked",
                    "next_tool": None,
                    "recovery_blocked": True,
                    "issues": [str(issue)],
                    "directive": (
                        "End the provider stream. A tool result could not be "
                        "bound to exactly one stream-owned tool invocation."
                    ),
                }
                msg = (
                    "Provider tool-result identity failed closed: "
                    f"{issue}."
                )
                try:
                    log_system_event(
                        "pipeline.provider_tool_result_binding_blocked",
                        "error",
                        msg,
                        handoff,
                    )
                except Exception:
                    pass
                raise _OrchActionableStageHandoff(msg, handoff)

            def _terminal_result_for_bound_tool(tool_use_id, content):
                """Accept terminal proof only from its exact mutating owner call."""

                owner = _normalized_provider_tool_name(
                    _pending_tool_uses.get(tool_use_id)
                )
                terminal = _completed_abandon_tool_result(content)
                if (
                    terminal is not None
                    and owner not in _TERMINAL_ABANDON_RESULT_OWNER_TOOLS
                ):
                    _raise_stream_result_binding_blocked(
                        "terminal_abandon_result_owner_mismatch:"
                        f"{owner or 'unknown'}"
                    )
                if terminal is not None:
                    cached_result = _validated_cached_terminal_for_owner(
                        owner,
                        tool_use_id,
                    )
                    if (
                        cached_result is not None
                        and _canonical_json_bytes(cached_result)
                        != _canonical_json_bytes(terminal)
                    ):
                        _raise_stream_result_binding_blocked(
                            "provider_terminal_cache_sdk_result_mismatch"
                        )
                return terminal

            if getattr(opts, "resume", None) is not None:
                raise RuntimeError("orchestrator_provider_session_resume_forbidden")
            provider_attempt = create_owned_provider_attempt(prompt, opts)
            _attempt_ref[0] = provider_attempt
            provider_token = activate_owned_provider_attempt(provider_attempt)
            native_match_dispatch_token = None
            _orchestrator_provider_stream_active = True
            try:
                # Bind native liveness to this exact SDK transport before any
                # MCP tool handler is allowed to inherit the task context.
                # This task resets only its ContextVar.  The outer bounded
                # stream owner retains registry authority through final
                # live/terminal proof, then revokes it on every return/error/
                # cancellation path.
                from pipeline_state import activate_native_match_dispatch_nonce

                native_match_dispatch_token = (
                    activate_native_match_dispatch_nonce(
                        str(provider_attempt.get("attempt_id") or "")
                    )
                )
                gen = claude_query(
                    prompt=prompt,
                    options=opts,
                    transport=owned_provider_attempt_transport(provider_attempt),
                )
                _gen_ref[0] = gen
                _stream_iter = gen.__aiter__()
                _first_activity_seen = False
                _stream_started_at = time.time()
                _last_message_at = _stream_started_at  # D: for stall ceiling
                while True:
                    try:
                        if not _first_activity_seen:
                            message = await await_provider_stream_next_bounded(
                                _stream_iter,
                                ORCH_FIRST_ACTIVITY_TIMEOUT,
                            )
                            _first_activity_seen = True
                            _last_message_at = time.time()
                            _first_latency = time.time() - _stream_started_at
                            if _first_latency >= 60:
                                try:
                                    log_system_event(
                                        "pipeline.first_activity_delayed", "warn",
                                        f"Orchestrator first stream activity after {_first_latency:.1f}s",
                                        {"latency_s": round(_first_latency, 1),
                                         "timeout_s": ORCH_FIRST_ACTIVITY_TIMEOUT},
                                    )
                                except Exception:
                                    pass
                        else:
                            message = await _await_next_stream_message(
                                _stream_iter,
                                last_message_at=_last_message_at,
                                stream_started_at=_stream_started_at,
                                baseline_owned_route_identity=(
                                    baseline_owned_route_identity
                                ),
                            )
                            _last_message_at = time.time()
                    except StopAsyncIteration:
                        if _pending_tool_uses:
                            cached_terminal = (
                                _settle_missing_terminal_result_from_cache()
                            )
                            if _pending_tool_uses:
                                _raise_stream_result_binding_blocked(
                                    "provider_stream_ended_with_pending_tool_results"
                                )
                            _terminal_tool_result_for_batch = (
                                cached_terminal
                                or _terminal_tool_result_for_batch
                            )
                        _raise_actionable_handoff_if_ready(
                            terminal_tool_result=_terminal_tool_result_for_batch
                        )
                        _terminal_tool_result_for_batch = None
                        break
                    except asyncio.TimeoutError as e:
                        if not _first_activity_seen:
                            msg = (
                                f"Orchestrator LLM produced no first stream message within "
                                f"{ORCH_FIRST_ACTIVITY_TIMEOUT}s"
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] {msg} — treating as infrastructure stall; "
                                    "checkpoint will be preserved for retry.",
                                    "warn",
                                )
                            try:
                                log_system_event(
                                    "pipeline.first_activity_timeout", "warn", msg,
                                    {"timeout_s": ORCH_FIRST_ACTIVITY_TIMEOUT,
                                     "session_present": bool(_load_orchestrator_session())},
                                )
                            except Exception:
                                pass
                            raise _OrchFirstActivityTimeout(msg) from e
                        raise
                    if isinstance(message, AssistantMessage):
                        assistant_has_tool_use = False
                        assistant_terminal_result = None
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                availability_trace.observe_text(block.text)
                                texts.append(block.text)
                                if ui:
                                    ui.log_io(block.text, "claude", "Orchestrator")
                                else:
                                    log.debug("%s", block.text.rstrip())
                                lf.write(block.text)
                                # Detect embedded API errors in text output (mid-stream visibility)
                                _text_lower = block.text.lower()
                                if 'internal server error' in _text_lower or 'api error' in _text_lower or 'internal network failure' in _text_lower:
                                    log.warning("Embedded API error detected in LLM output: %s", block.text[:200])
                                    if ui:
                                        ui.log_history("[Orchestrator] Mid-stream API error detected", "warning")
                            elif isinstance(block, ToolUseBlock):
                                assistant_has_tool_use = True
                                _register_pending_tool_use(
                                    block,
                                    source="assistant",
                                )
                            elif isinstance(block, ThinkingBlock):
                                thinking = block.thinking or "[thinking...]"
                                if ui:
                                    ui.log_io(thinking, "thinking", "Orchestrator")
                                else:
                                    log.debug("[thinking...]")
                                lf.write(f"\n[THINKING] {thinking[:2000]}\n")
                            elif isinstance(block, ToolResultBlock):
                                content = block.content if isinstance(block.content, str) else (
                                    json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                                )
                                if content:
                                    lf.write(f"\n[tool_result] {content[:500]}\n")
                                    if ui:
                                        ui.log_io(content[:3000], "tool_result", "Orchestrator")
                                _raise_for_llm_availability_tool_result(
                                    block.content
                                )
                                tool_use_id = str(
                                    getattr(block, "tool_use_id", "") or ""
                                )
                                if tool_use_id not in _pending_tool_uses:
                                    _raise_stream_result_binding_blocked(
                                        "assistant_tool_result_id_unknown_or_duplicate"
                                    )
                                assistant_terminal_result = (
                                    _terminal_result_for_bound_tool(
                                        tool_use_id,
                                        block.content,
                                    )
                                    or assistant_terminal_result
                                )
                                _terminal_tool_result_for_batch = (
                                    assistant_terminal_result
                                    or _terminal_tool_result_for_batch
                                )
                                _pending_tool_uses.pop(tool_use_id, None)
                                _settle_registered_evolution_tool_use(
                                    tool_use_id
                                )
                                # A nested role may have converted the typed
                                # exception into its legacy infra payload. The
                                # shared run_claude_query boundary persists the
                                # pause first, so stop this parent stream before
                                # the model can call another tool and consume a
                                # second gate attempt.
                                nested_pause = active_llm_pause()
                                if nested_pause is not None:
                                    raise blocked_from_pause_state(
                                        nested_pause,
                                        role="Orchestrator",
                                    )
                        # Sub-agent costs have settled in the durable generation
                        # ledger by this point.  Default mode only emits telemetry;
                        # an explicit operator hard limit stops the stream.
                        _check_generation_cost_policy(ui)
                        if not assistant_has_tool_use and not _pending_tool_uses:
                            _raise_actionable_handoff_if_ready(
                                terminal_tool_result=(
                                    assistant_terminal_result
                                    or _terminal_tool_result_for_batch
                                ),
                            )
                            _terminal_tool_result_for_batch = None
                    elif isinstance(message, UserMessage):
                        nested_pause = active_llm_pause()
                        if nested_pause is not None:
                            raise blocked_from_pause_state(
                                nested_pause,
                                role="Orchestrator",
                            )
                        saw_tool_result = False
                        terminal_tool_result = None
                        if isinstance(message.content, list):
                            for block in message.content:
                                # The SDK parser permits ToolUseBlock in a
                                # UserMessage.  Treat it identically to the
                                # AssistantMessage placement; otherwise the
                                # real handler can mutate/abandon a generation
                                # while the parent has no pending id to bind its
                                # eventual result to.
                                if isinstance(block, ToolUseBlock):
                                    _register_pending_tool_use(
                                        block,
                                        source="user",
                                    )
                                    continue
                                if not isinstance(block, ToolResultBlock):
                                    continue
                                saw_tool_result = True
                                content = block.content
                                rendered = (
                                    content
                                    if isinstance(content, str)
                                    else json.dumps(content, ensure_ascii=False)
                                    if content is not None
                                    else ""
                                )
                                if rendered:
                                    lf.write(f"\n[tool_result] {rendered[:500]}\n")
                                    if ui:
                                        ui.log_io(
                                            rendered[:3000],
                                            "tool_result",
                                            "Orchestrator",
                                )
                                _raise_for_llm_availability_tool_result(content)
                                tool_use_id = str(
                                    getattr(block, "tool_use_id", "") or ""
                                )
                                if tool_use_id in _ignored_user_tool_use_ids:
                                    _ignored_user_tool_use_ids.discard(tool_use_id)
                                    continue
                                if tool_use_id not in _pending_tool_uses:
                                    _raise_stream_result_binding_blocked(
                                        "user_tool_result_id_unknown_or_duplicate"
                                    )
                                terminal_tool_result = (
                                    _terminal_result_for_bound_tool(
                                        tool_use_id,
                                        content,
                                    )
                                    or terminal_tool_result
                                )
                                _terminal_tool_result_for_batch = (
                                    terminal_tool_result
                                    or _terminal_tool_result_for_batch
                                )
                                _pending_tool_uses.pop(tool_use_id, None)
                                _settle_registered_evolution_tool_use(
                                    tool_use_id
                                )
                        tool_use_result = getattr(
                            message,
                            "tool_use_result",
                            None,
                        )
                        if tool_use_result is not None:
                            if not saw_tool_result:
                                _raise_for_llm_availability_tool_result(
                                    tool_use_result
                                )
                            fallback_tool_use_id = ""
                            if isinstance(tool_use_result, dict):
                                fallback_tool_use_id = str(
                                    tool_use_result.get("tool_use_id") or ""
                                )
                            if not fallback_tool_use_id:
                                fallback_tool_use_id = str(
                                    getattr(
                                        message,
                                        "parent_tool_use_id",
                                        "",
                                    )
                                    or ""
                                )
                            if (
                                not fallback_tool_use_id
                                and len(_pending_tool_uses) == 1
                            ):
                                fallback_tool_use_id = next(
                                    iter(_pending_tool_uses)
                                )
                            if fallback_tool_use_id in _ignored_user_tool_use_ids:
                                # Keep the legacy ``tool_use_result`` shortcut
                                # consistent with ToolResultBlock handling:
                                # a non-Evolution annotation carried by a
                                # UserMessage has no gate lease and cannot be
                                # promoted into a missing-result failure.
                                _ignored_user_tool_use_ids.discard(
                                    fallback_tool_use_id
                                )
                            elif fallback_tool_use_id in _pending_tool_uses:
                                terminal_tool_result = (
                                    _terminal_result_for_bound_tool(
                                        fallback_tool_use_id,
                                        tool_use_result,
                                    )
                                    or terminal_tool_result
                                )
                                _terminal_tool_result_for_batch = (
                                    terminal_tool_result
                                    or _terminal_tool_result_for_batch
                                )
                                _pending_tool_uses.pop(
                                    fallback_tool_use_id,
                                    None,
                                )
                                _settle_registered_evolution_tool_use(
                                    fallback_tool_use_id
                                )
                            elif not saw_tool_result:
                                _raise_stream_result_binding_blocked(
                                    "user_tool_use_result_id_unknown_or_duplicate"
                                )
                        nested_pause = active_llm_pause()
                        if nested_pause is not None:
                            raise blocked_from_pause_state(
                                nested_pause,
                                role="Orchestrator",
                            )
                        _check_generation_cost_policy(ui)
                        if not _pending_tool_uses:
                            _raise_actionable_handoff_if_ready(
                                terminal_tool_result=(
                                    terminal_tool_result
                                    or _terminal_tool_result_for_batch
                                ),
                            )
                            _terminal_tool_result_for_batch = None
                    elif isinstance(message, ResultMessage):
                        availability_trace.observe_result(message)
                        billing_status = record_generation_cost(
                            "Orchestrator",
                            message.total_cost_usd,
                            getattr(message, "usage", None),
                            source="orchestrator_result",
                            event_id=sdk_result_event_id(
                                message,
                                source="orchestrator_result",
                                attempt=stream_invocation_id,
                            ),
                        )
                        # A resumed SDK stream may replay the same Result.
                        # Count/UI-project only the first durable occurrence.
                        billing_new = (
                            not billing_status.get("active")
                            or billing_status.get("recorded")
                            or billing_status.get("pending_only")
                        )
                        if billing_new:
                            if message.total_cost_usd is not None:
                                cost += message.total_cost_usd
                            if ui:
                                ui.update_cost(
                                    "Orchestrator",
                                    float(message.total_cost_usd or 0.0),
                                    getattr(message, "usage", None),
                                )
                        _check_generation_cost_policy(ui)
                        if not message.is_error:
                            if _pending_tool_uses:
                                cached_terminal = (
                                    _settle_missing_terminal_result_from_cache()
                                )
                                if _pending_tool_uses:
                                    _raise_stream_result_binding_blocked(
                                        "provider_result_with_pending_tool_results"
                                    )
                                _terminal_tool_result_for_batch = (
                                    cached_terminal
                                    or _terminal_tool_result_for_batch
                                )
                            _raise_actionable_handoff_if_ready(
                                terminal_tool_result=(
                                    _terminal_tool_result_for_batch
                                ),
                            )
                            _terminal_tool_result_for_batch = None
                            ok = True
                            if message.session_id:
                                _save_orchestrator_session(message.session_id)
                        else:
                            error_text = extract_result_error(message)
                            lf.write(f"\n[API ERROR] {error_text}\n")
                            if ui:
                                ui.log_history(f"[Orchestrator] API error: {error_text[:200]}", "error")
                            # Parse a provider-declared quota reset.  The next
                            # attempt is a fresh stream over the same validated
                            # checkpoint; opaque provider history is not retained.
                            is_429 = "429" in error_text or ("已达到" in error_text and "使用上限" in error_text)
                            if is_429:
                                from rate_limiter import rate_limiter
                                rate_limiter.parse_429(error_text)
                            else:
                                _clear_orchestrator_session()
                            # Detect real auth failures. Match status tokens, NOT bare substrings —
                            # otherwise cost strings like "$0.4017"/"$0.4031" and token counts falsely
                            # trip auth_err, misrouting signature/infra failures into the 300s auth
                            # backoff path (which itself never fires on a real 401/403 in practice).
                            import re as _re
                            if _re.search(r"\b40[13]\b", error_text) or \
                                    "invalid x-api-key" in error_text.lower() or \
                                    "authentication" in error_text.lower():
                                auth_err = True
                            availability_block = availability_trace.blocked(
                                role="Orchestrator"
                            )
                            if availability_block is not None:
                                raise availability_block
            except LLMAvailabilityBlocked:
                raise
            except (
                _OrchActionableStageHandoff,
                OperatorGenerationCostLimitExceeded,
            ):
                raise
            except (CLINotFoundError, ProcessError) as e:
                availability_block = availability_trace.blocked(
                    role="Orchestrator",
                    exception=e,
                )
                if availability_block is not None:
                    raise availability_block from e
                if ui:
                    ui.log_io(f"[ERROR] {e}", "error", "Orchestrator")
                else:
                    log.error("LLM error: %s", e)
                # Propagate to the outer `except Exception` so is_llm_infra_error
                # classification -> -0.5 infra sentinel applies, instead of falling
                # through and returning cost=0/ok=False (fake $0 success that masks
                # exit-143 / ProcessError crashes). The OUTER except (commit 0295d2b)
                # was fixed but this INNER one was missed (same shape as v84 deadlock).
                raise
            except ClaudeSDKError as _sig_err:
                availability_block = availability_trace.blocked(
                    role="Orchestrator",
                    exception=_sig_err,
                )
                if availability_block is not None:
                    raise availability_block from _sig_err
                # Signature-field stream errors: transient SDK deserialization bug
                # (ThinkingBlock.signature missing). Retryable ONLY when no MCP tool
                # has executed yet — otherwise tool side-effects would be duplicated.
                # When retryable, convert to _OrchSignatureRetryable for the retry
                # loop in _run_one_cycle; otherwise propagate (-> infra -0.5 backoff).
                _sig_s = str(_sig_err).lower()
                if ("signature" in _sig_s or "missing required field" in _sig_s) and not _tool_call_counts:
                    raise _OrchSignatureRetryable(str(_sig_err)) from _sig_err
                raise
            except Exception as e:
                availability_block = availability_trace.blocked(
                    role="Orchestrator",
                    exception=e,
                )
                if availability_block is not None:
                    raise availability_block from e
                raise
            finally:
                try:
                    if gen is not None:
                        await cleanup_owned_provider_attempt(
                            gen,
                            provider_attempt,
                            "ORCHESTRATOR",
                            log_file,
                        )
                finally:
                    _orchestrator_provider_stream_active = False
                    if native_match_dispatch_token is not None:
                        try:
                            from pipeline_state import reset_native_match_dispatch_nonce

                            reset_native_match_dispatch_nonce(
                                native_match_dispatch_token
                            )
                        except Exception:
                            pass
                    reset_owned_provider_attempt(provider_token)
            if _tool_call_counts:
                log.info("Tool call summary: %s", dict(sorted(_tool_call_counts.items())))
            return "".join(texts), cost, ok, gen, auth_err

        CYCLE_TIMEOUT = 14400  # 240 minutes (4h) max per LLM cycle. Generous for GLM-5.2 variable output speed: during peak provider load a single Scout can take 15-20min. Full pipeline (Scout ensemble + Critics + final + workers + review + critic + precommit) can reach 2-3h under load. 14400s gives ample room without being unbounded.
        # Sentinel returned by the timeout-extension path (stage=verified, first extension).
        # Must be DISTINCT from every other cost signal: -0.5 (infra), -1.0 (generic crash),
        # and the auth clamp -max(abs(total_cost), 1.0) which can reach any negative value
        # ≥1.0 in magnitude. -99999.0 is unreachable in practice (a single cycle cannot
        # spend $99999) so it cannot collide with the auth clamp even if a future cycle's
        # total_cost grew large. This fixes the v101 death-loop's latent collision risk.
        _TIMEOUT_EXTENSION_SENTINEL = -99999.0
        query_gen = None
        # Mutable container to track the async generator across scope boundaries.
        # The bounded task owner raises before tuple unpacking on timeout, so the
        # exact generator is published here from inside _stream_response.
        _gen_ref = [None]
        # The complete owned-attempt record is published before SDK dispatch so
        # the cycle-level timeout can terminate and verify this exact transport.
        _attempt_ref = [None]
        # H1: rotate a previously-cancelled precommit attempt token at cycle
        # start; never clear or revive the detached old attempt.
        try:
            from tool_eval import reset_precommit_shutdown
            reset_precommit_shutdown()
        except Exception:
            pass
        try:
            try:
                # Signature-retry loop for the orchestrator's own SDK stream.
                # _stream_response raises _OrchSignatureRetryable when a transient
                # signature-field error occurs AND no MCP tool has executed yet
                # (no side-effects to duplicate). Bounded to _ORCH_SIG_MAX_ATTEMPTS.
                _ORCH_SIG_MAX_ATTEMPTS = 2
                full_output, total_cost, cycle_completed, query_gen, auth_error = (
                    None, 0.0, False, None, False
                )
                # NOTE: each retry attempt is individually wrapped by CYCLE_TIMEOUT
                # below, so worst-case wall-clock is (N+1)*CYCLE_TIMEOUT. This is
                # acceptable because (a) signature errors fire early in the stream
                # (ThinkingBlock deserialization), so each attempt burns little of
                # the 3600s budget, and (b) the watchdog/stuck-pipeline detector
                # remains the hard backstop. Signature retries only happen when no
                # MCP tool has executed yet (no side-effects to duplicate).
                for _sig_attempt in range(_ORCH_SIG_MAX_ATTEMPTS + 1):
                    try:
                        full_output, total_cost, cycle_completed, query_gen, auth_error = (
                            await _await_orchestrator_stream_response_bounded(
                                _stream_response(options),
                                timeout=CYCLE_TIMEOUT,
                                attempt_ref=_attempt_ref,
                                gen_ref=_gen_ref,
                                log_file_path=log_file,
                            )
                        )
                        break
                    except _OrchSignatureRetryable as _sr:
                        if _sig_attempt < _ORCH_SIG_MAX_ATTEMPTS:
                            _backoff = min(5 * (2 ** _sig_attempt), 20)
                            log.warning(
                                "Orchestrator SDK signature stream error (attempt %d/%d), "
                                "retrying in %ds (no tool side-effects yet): %s",
                                _sig_attempt + 1, _ORCH_SIG_MAX_ATTEMPTS + 1, _backoff, _sr,
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] SDK signature stream error — retrying "
                                    f"(attempt {_sig_attempt + 1}/{_ORCH_SIG_MAX_ATTEMPTS + 1})...",
                                    "warn",
                                )
                            try:
                                log_system_event("pipeline.orch_signature_retry", "warn",
                                    f"Orchestrator signature stream retry (attempt {_sig_attempt + 1})",
                                    {"attempt": _sig_attempt + 1, "error": str(_sr)[:200]})
                            except Exception:
                                pass
                            await asyncio.sleep(_backoff)
                            continue
                        # Exhausted — re-raise the ORIGINAL ClaudeSDKError (stored
                        # as __cause__ when _OrchSignatureRetryable was raised) so
                        # the outer except classifies it as infra (-0.5 backoff)
                        # via type-based is_llm_infra_error, not only the keyword
                        # fallback (which would silently break if the keyword
                        # guard were ever tightened).
                        raise (_sr.__cause__ or _sr) from None
                if full_output is None:
                    # All signature retries exhausted and re-raised above; defensive.
                    raise RuntimeError("orchestrator signature retry loop exited without result")
            except asyncio.TimeoutError:
                # Signal the exact in-flight native precommit attempt to stop.
                # The owned provider stream is already cancelled, but a complete
                # 70-hand subprocess-backed match is the smallest interruptible
                # evidence unit.  The monotonic token is checked after that unit
                # and before another sample can launch or reach a terminal gate.
                try:
                    from tool_eval import set_precommit_shutdown
                    set_precommit_shutdown()
                except Exception as _se:
                    log.debug("set_precommit_shutdown failed: %s", _se)

                # Stage-aware timeout skip: if pipeline is at the "verified" stage,
                # commit is the next gate (idempotent) — grant ONE extension.
                # Only "verified" (precommit passed) qualifies — "critic_checked" still
                # has verified + archived before commit, so granting there produced the
                # v101 false-complete death loop.
                try:
                    from evolution_core import read_pipeline_checkpoint as _read_ckpt
                    _ckpt = _read_ckpt()
                    if _ckpt and _ckpt.get("stage") == "publishing":
                        # Publication crossed a durable one-way boundary. A
                        # provider/session timeout cannot rewrite it to
                        # ``timed_out`` or abandon it; the next fresh session
                        # must reconcile the same immutable intent.
                        _clear_orchestrator_session()
                        try:
                            _write_timeout_checkpoint_from_exact_snapshot(
                                _ckpt,
                                "publishing",
                                touch_stage_timestamp=True,
                            )
                        except Exception:
                            pass
                        lf.write(
                            "\n[TIMEOUT] Durable publication preserved; "
                            "resuming the same intent.\n"
                        )
                        return _TIMEOUT_EXTENSION_SENTINEL
                    if _ckpt and _ckpt.get("stage") == "verified":
                        # ONE extension only: a per-version counter persisted in the
                        # checkpoint prevents every timeout at this stage re-granting.
                        _ext_count = _ckpt.get("timeout_extensions", 0)
                        if _ext_count >= 1:
                            # Already used the single extension — fall through to normal
                            # timeout handling below (marks timed_out, restarts cycle).
                            log.warning(
                                "Cycle timeout at stage=verified but timeout_extensions=%d already used — NOT granting (one extension limit).",
                                _ext_count,
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] Cycle timeout at stage=verified but the single "
                                    f"timeout extension was already granted (count={_ext_count}). "
                                    f"Not granting again.",
                                    "warn",
                                )
                            lf.write(f"\n[TIMEOUT] Stage=verified, extension already used ({_ext_count}) — not granting.\n")
                        else:
                            log.warning(
                                "Cycle timeout at stage=%s — commit is imminent, granting ONE extension (idempotent recovery)",
                                _ckpt.get("stage"),
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] Cycle timeout at stage={_ckpt.get('stage')} — "
                                    f"commit imminent, granting ONE extension.",
                                    "warn",
                                )
                            lf.write(f"\n[TIMEOUT] Stage={_ckpt.get('stage')} — granting ONE extension (commit imminent)\n")
                            # The owned stream boundary has confirmed or fenced
                            # generator/process cleanup. The disposable session
                            # cannot be resumed; restart from the checkpoint.
                            _clear_orchestrator_session()
                            # Refresh checkpoint timestamp so the watchdog does not immediately
                            # re-trigger on the next cycle (elapsed > WATCHDOG_TIMEOUT), AND
                            # record the single granted extension (timeout_extensions=1).
                            try:
                                _write_timeout_checkpoint_from_exact_snapshot(
                                    _ckpt,
                                    _ckpt.get("stage"),
                                    master_plan=_ckpt.get("master_plan"),
                                    reviewer_feedback=_ckpt.get("reviewer_feedback", ""),
                                    generation_attempt=_ckpt.get("generation_attempt", 0),
                                    gate_results=_ckpt.get("gate_results", {}) or {},
                                    parent2_v=_ckpt.get("parent2_v"),
                                    direction_audit=_ckpt.get("direction_audit"),
                                    audit_context=_ckpt.get("audit_context", {}) or {},
                                    audit_attempt=_ckpt.get("audit_attempt", 0),
                                    precommit_attempt=_ckpt.get("precommit_attempt", 0),
                                    timeout_extensions=1,
                                    touch_stage_timestamp=True,
                                )
                            except Exception:
                                pass  # Non-fatal: watchdog may trigger, but checkpoint is preserved
                            # Sentinel _TIMEOUT_EXTENSION_SENTINEL = "timeout extension granted,
                            # cycle NOT complete". The main loop treats it distinctly: NO
                            # post_generation_cleanup, NO 'gen complete' log, NO backoff — just
                            # resume from the checkpoint. Value -99999.0 is mathematically
                            # distinct from -0.5 infra / -1.0 generic / auth clamp (≥1.0 magnitude).
                            return _TIMEOUT_EXTENSION_SENTINEL
                except Exception:
                    pass  # If checkpoint read fails, fall through to normal timeout handling

                if ui:
                    ui.log_history(
                        f"[Orchestrator] Cycle timed out after {CYCLE_TIMEOUT}s — killing stuck session.",
                        "error",
                    )
                else:
                    log.error("Cycle timed out after %ss", CYCLE_TIMEOUT)
                lf.write(f"\n[TIMEOUT] Cycle killed after {CYCLE_TIMEOUT}s\n")
                _clear_orchestrator_session()
                # Mark pipeline checkpoint as timed_out so next cycle doesn't repeat
                # the same stuck state (e.g., repeatedly failing run_precommit_eval)
                ckpt = None
                try:
                    from evolution_core import read_pipeline_checkpoint
                    ckpt = read_pipeline_checkpoint()
                    if ckpt and ckpt.get("stage") not in ("timed_out", "archived"):
                        # B3 (v125 retry-storm fix): if Master repeatedly failed this
                        # cycle (audit_attempt >= MAX_MASTER_TOTAL_FAILURES=2), the
                        # timeout was caused by the Master retry-storm itself — abandon
                        # now instead of marking timed_out + resuming into the same
                        # stuck Master loop. "verified" is excluded (it has its own
                        # extension path above; commit is the imminent idempotent step).
                        _b3_audit = int(ckpt.get("audit_attempt") or 0)
                        _b3_stage = ckpt.get("stage")
                        _B3_MASTER_FAIL_THRESHOLD = 2  # mirrors MAX_MASTER_TOTAL_FAILURES (tool_planning.py)
                        if (
                            _b3_audit >= _B3_MASTER_FAIL_THRESHOLD
                            and _b3_stage == "direction_audited"
                        ):
                            log.warning(
                                "Cycle timed out with Master fail count=%d (stage=%s) — "
                                "abandoning stuck generation instead of marking timed_out.",
                                _b3_audit, _b3_stage,
                            )
                            log_system_event(
                                "pipeline.cycle_timeout_abandon", "error",
                                f"Cycle timed out after {CYCLE_TIMEOUT}s with Master fail count={_b3_audit} — abandoning",
                                {"timeout_sec": CYCLE_TIMEOUT, "pipeline_stage": _b3_stage,
                                 "master_fail_count": _b3_audit},
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] Cycle timed out after Master failed "
                                    f"{_b3_audit}× — abandoning stuck generation "
                                    f"(instead of marking timed_out + resuming).",
                                    "error",
                                )
                            try:
                                from tool_bot_management import (
                                    _do_abandon_generation,
                                    expected_abandon_identity,
                                )
                                abandon_result = await _do_abandon_generation(
                                    reason=f"cycle_timeout_master_stuck ({_b3_audit} fails)",
                                    _bypass_rate_limit=True,
                                    **expected_abandon_identity(ckpt),
                                )
                                terminal_result = _completed_abandon_tool_result(
                                    abandon_result
                                )
                                if terminal_result is None:
                                    raise RuntimeError(
                                        "cycle_timeout_master_abandon_not_completed"
                                    )
                                from tool_bot_management import (
                                    validate_completed_abandon_handoff,
                                )

                                terminal_proof = validate_completed_abandon_handoff(
                                    ckpt,
                                    terminal_result,
                                )
                                if (
                                    gen_ctx is not None
                                    and not _remember_verified_canonical_abandon(
                                        gen_ctx,
                                        terminal_proof,
                                    )
                                ):
                                    raise RuntimeError(
                                        "cycle_timeout_master_abandon_proof_"
                                        "context_mismatch"
                                    )
                                return ORCH_GENERATION_ABANDONED_COST
                            except Exception as _ae:
                                log.error(
                                    "B3 canonical abandon failed closed: %s",
                                    _ae,
                                )
                                return ORCH_RECOVERY_BLOCKED_COST
                        else:
                            # v193 root-cause-audit (2026-06-26): distinguish an
                            # INFRA-only timeout from a real regression. When the
                            # cycle timed out during precommit (i.e. quality +
                            # review + critic already passed, stage=critic_checked)
                            # AND precommit produced no regression blocker (the bot
                            # code itself is fine — the timeout was caused by the
                            # daemon/scheduler failing to deliver battle results),
                            # mark `infra_timed_out` instead of `timed_out`. The
                            # recovery handler then RETRIES precommit on the SAME
                            # code instead of clearing the checkpoint and discarding
                            # the generation (which wasted v193's already-passed
                            # gates). If any regression blocker exists, fall back to
                            # plain timed_out (a real regression should not be retried).
                            _gate_results = ckpt.get("gate_results", {}) or {}
                            _is_precommit_stage = _b3_stage == "critic_checked"
                            _has_precommit_regression = False
                            _pc = _gate_results.get("precommit_eval") or {}
                            if isinstance(_pc, dict):
                                for _b in (_pc.get("blockers") or []):
                                    _reason = (_b.get("reason") if isinstance(_b, dict) else _b) or ""
                                    if _reason not in _INFRA_BLOCKER_REASONS_SET:
                                        _has_precommit_regression = True
                                        break
                            _infra_only = (
                                _is_precommit_stage
                                and _gate_results.get("quality", {}).get("passed")
                                and _gate_results.get("review", {}).get("passed")
                                and _gate_results.get("critic", {}).get("passed")
                                and not _has_precommit_regression
                            )
                            if _infra_only:
                                marked_timeout = _write_timeout_checkpoint_from_exact_snapshot(
                                    ckpt,
                                    "infra_timed_out",
                                    master_plan=ckpt.get("master_plan"),
                                )
                                timeout_message = (
                                    "Infra-only precommit timeout recorded; the exact "
                                    "candidate/gates will be re-proven before retry."
                                    if marked_timeout
                                    else "Infra-timeout overlay lost its checkpoint CAS; "
                                    "newer checkpoint authority was preserved."
                                )
                                log.warning(timeout_message)
                                log_system_event(
                                    "pipeline.cycle_timeout_infra"
                                    if marked_timeout
                                    else "pipeline.cycle_timeout_stage_preserved",
                                    "warn",
                                    timeout_message,
                                    {
                                        "timeout_sec": CYCLE_TIMEOUT,
                                        "pipeline_stage": _b3_stage,
                                        "precommit_attempt": ckpt.get(
                                            "precommit_attempt", 0
                                        ),
                                        "timeout_overlay_applied": marked_timeout,
                                    },
                                )
                                if ui:
                                    ui.log_history(
                                        f"[Orchestrator] {timeout_message}",
                                        "warn",
                                    )
                            else:
                                # LOG GAP FIX (2026-06-30): plain timed_out (the most
                                # common timeout path) previously had NO structured
                                # event — only cycle_timeout_abandon/infra logged.
                                # Record stage + reason so timeouts are auditable.
                                marked_timeout = _write_timeout_checkpoint_from_exact_snapshot(
                                    ckpt,
                                    "timed_out",
                                    master_plan=ckpt.get("master_plan"),
                                )
                                timeout_message = (
                                    f"Cycle timed out after {CYCLE_TIMEOUT}s at "
                                    f"disposable stage={_b3_stage}; canonical "
                                    "abandon is now required."
                                    if marked_timeout
                                    else f"Cycle timed out after {CYCLE_TIMEOUT}s at "
                                    f"stage={_b3_stage}; exact stage/newer checkpoint "
                                    "authority was preserved."
                                )
                                try:
                                    log_system_event(
                                        "pipeline.cycle_timeout_plain"
                                        if marked_timeout
                                        else "pipeline.cycle_timeout_stage_preserved",
                                        "error" if marked_timeout else "warn",
                                        timeout_message,
                                        {
                                            "timeout_sec": CYCLE_TIMEOUT,
                                            "pipeline_stage": _b3_stage,
                                            "next_v": ckpt.get("next_v"),
                                            "source_v": ckpt.get("source_v"),
                                            "precommit_attempt": ckpt.get(
                                                "precommit_attempt", 0
                                            ),
                                            "master_fail_count": _b3_audit,
                                            "timeout_overlay_applied": marked_timeout,
                                        },
                                    )
                                except Exception:
                                    pass
                                if ui:
                                    ui.log_history(
                                        f"[Orchestrator] {timeout_message}",
                                        "error" if marked_timeout else "warn",
                                    )
                except Exception as e:
                    log.warning("Failed to mark checkpoint timed_out: %s", e)
                try:
                    log_system_event("pipeline.cycle_timeout", "error",
                        f"Orchestrator cycle timed out after {CYCLE_TIMEOUT}s",
                        {"timeout_sec": CYCLE_TIMEOUT,
                         "pipeline_stage": ckpt.get("stage") if ckpt else "unknown"})
                except Exception:
                    pass
                if ui:
                    return ui.gen_cost_total - _cost_at_start
                return total_cost

            # 529 rate-limit retry with exponential backoff
            if (
                not cycle_completed
                and looks_like_provider_error_envelope(full_output)
                and _is_rate_limited(full_output)
            ):
                # Retry from the same sealed prompt and typed checkpoint/MCP
                # projection, but never from opaque provider conversation history.
                _clear_orchestrator_session()
                retry_opts = ClaudeAgentOptions(
                    model="sonnet",
                    permission_mode="bypassPermissions",
                    cwd=str(PROJECT_ROOT),
                    mcp_servers={"evolution": evolution_server},
                    strict_mcp_config=True,
                    tools=[],
                    disallowed_tools=_BLOCKED_MCP_TOOLS,
                    hooks={**_make_precompact_hook(), **_make_bot_dir_guard_hook()},
                    max_turns=max_turns,
                    thinking={"type": "adaptive"},  # let Claude decide thinking depth
                )
                for backoff in [30, 60, 120]:
                    if ui:
                        ui.log_history(f"Orchestrator rate limited (529). Retrying in {backoff}s...", "warn")
                    lf.write(f"\n[529 RETRY] backing off {backoff}s\n")
                    if shutdown_mgr:
                        try:
                            await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=backoff)
                            return total_cost
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(backoff)
                    try:
                        full_output, retry_cost, cycle_completed, query_gen, auth_error = (
                            await _await_orchestrator_stream_response_bounded(
                                _stream_response(retry_opts),
                                timeout=CYCLE_TIMEOUT,
                                attempt_ref=_attempt_ref,
                                gen_ref=_gen_ref,
                                log_file_path=log_file,
                            )
                        )
                    except asyncio.TimeoutError:
                        raise  # Re-raise to outer timeout handler
                    total_cost += retry_cost
                    if not (
                        looks_like_provider_error_envelope(full_output)
                        and _is_rate_limited(full_output)
                    ):
                        break
                else:
                    # Every attempt used the same sealed checkpoint/prompt
                    # projection but a fresh provider stream.  Exhaustion is
                    # an infrastructure failure, never a successful cycle and
                    # never a reason to recover opaque provider history.
                    cycle_failed = True
                    infra_error = True
                    log.warning(
                        "529 retries exhausted; checkpoint preserved and "
                        "provider history discarded"
                    )
                    if ui:
                        ui.log_history(
                            "[Orchestrator] 529 retries exhausted. Checkpoint "
                            "preserved; the next attempt will use a fresh "
                            "checkpoint-bound provider stream.",
                            "warn",
                        )
                    lf.write(
                        "\n[529 RETRIES EXHAUSTED] checkpoint preserved; "
                        "provider history discarded\n"
                    )

            # 429 quota detected — exit cycle cleanly so orchestrator_loop can block
            from rate_limiter import rate_limiter
            if rate_limiter.is_blocked() and not cycle_completed:
                if ui:
                    ui.log_history(
                        "[Orchestrator] 429 配额耗尽。Checkpoint 保留；"
                        "provider history 已丢弃，恢复后使用新 stream 继续。",
                        "warn",
                    )
                return (ui.gen_cost_total - _cost_at_start) if ui else total_cost

            if ui:
                total_cost = ui.gen_cost_total - _cost_at_start
            if not cycle_failed:
                lf.write(f"\n[CYCLE DONE] cost=${total_cost:.4f}\n")

        except KeyboardInterrupt:
            if ui:
                ui.log_history("[Orchestrator] Interrupted by user.", "warn")
            else:
                log.warning("Interrupted by user.")
            lf.write("\n[INTERRUPTED]\n")

        except asyncio.CancelledError:
            # Signal in-flight native precommit work to stop after its current
            # complete 70-hand evidence unit.
            try:
                from tool_eval import set_precommit_shutdown
                set_precommit_shutdown()
            except Exception:
                pass
            # The validated checkpoint is preserved; provider history is not.
            if ui:
                ui.log_history("[Orchestrator] Cancelled — checkpoint preserved for a fresh provider stream.", "warn")
            else:
                log.warning("Cancelled — checkpoint preserved for a fresh provider stream.")
            lf.write("\n[CANCELLED — checkpoint preserved; provider history discarded]\n")
            raise

        except _OrchActionableStageHandoff as e:
            # Normal pipeline handoff: a tool has already persisted a checkpoint
            # whose route_policy has a deterministic next tool. Stop the current
            # SDK stream and let the main loop route directly from the checkpoint.
            # _stream_response's finally has already completed owned cleanup.
            _clear_orchestrator_session(reason="actionable_stage_handoff")
            if ui:
                ui.log_history(f"[Orchestrator] {e}", "info")
            else:
                log.info("%s", e)
            lf.write(f"\n[ACTIONABLE_HANDOFF] {e}\n")
            if e.handoff.get("recovery_blocked") is True:
                return ORCH_RECOVERY_BLOCKED_COST
            if e.handoff.get("scheduler_handoff_required") is True:
                if (
                    gen_ctx is not None
                    and not _remember_verified_canonical_abandon(
                        gen_ctx,
                        e.handoff.get("terminal_proof"),
                    )
                ):
                    log.error(
                        "Canonical terminal handoff lacked an exact proof "
                        "bound to the active generation context."
                    )
                    return ORCH_RECOVERY_BLOCKED_COST
                return ORCH_GENERATION_ABANDONED_COST
            if e.handoff.get("operator_action_required") is True:
                return ORCH_OPERATOR_ACTION_REQUIRED_COST
            return ORCH_ACTIONABLE_HANDOFF_COST

        except OperatorGenerationCostLimitExceeded as e:
            # This is an operator-requested stop, not an API/SDK infrastructure
            # failure.  Preserve the generation checkpoint, discard the
            # disposable Claude session, and park the outer loop instead of
            # retrying every 15 seconds and spending past the same limit.
            _clear_orchestrator_session(reason="operator_generation_cost_limit")
            if ui:
                ui.set_status("Stopped: operator generation cost limit", is_working=False)
                _project_generation_cost_runtime(ui)
            lf.write(f"\n[OPERATOR_COST_LIMIT] {e}\n")
            return ORCH_OPERATOR_COST_LIMIT_COST

        except LLMAvailabilityBlocked as e:
            # Provider availability is control-plane state, not a failed bot or
            # a retryable SDK signature glitch. Owned cleanup has completed;
            # persist the typed pause and preserve every generation/Worker
            # checkpoint exactly as-is.
            _clear_orchestrator_session(reason="llm_availability_blocked")
            try:
                pause_state = persist_llm_pause(e)
            except Exception as pause_error:
                pause_state = None
                log.exception("Failed to persist LLM availability pause: %s", pause_error)
            issue = e.issue
            if ui:
                ui.log_history(
                    f"[Orchestrator] LLM unavailable ({issue.category}); "
                    "checkpoint preserved and retry loop stopped.",
                    "error",
                )
                ui.set_status(
                    f"LLM unavailable ({issue.category})",
                    is_working=False,
                )
            try:
                log_system_event(
                    "orchestrator.llm_availability_blocked",
                    "error",
                    f"Orchestrator paused for LLM availability: {issue.category}",
                    {
                        "availability_issue": issue.as_dict(),
                        "pause_persisted": bool(pause_state),
                        "checkpoint_preserved": True,
                        "worker_attempt_consumed": False,
                    },
                )
            except Exception:
                pass
            lf.write(
                f"\n[LLM_AVAILABILITY_BLOCKED] {issue.category}: "
                f"{issue.summary}\n"
            )
            return ORCH_LLM_AVAILABILITY_BLOCKED_COST

        except LLMAvailabilityPauseError as e:
            # The Worker was already fenced, but its global pause record could
            # not be proven. Stop the current stream and let the outer sentinel
            # path fail closed; never translate this into a generic retry.
            _clear_orchestrator_session(reason="llm_availability_state_invalid")
            if ui:
                ui.log_history(f"[Orchestrator] {e}", "error")
                ui.set_status("Stopped: invalid LLM availability state", is_working=False)
            try:
                log_system_event(
                    "orchestrator.llm_availability_state_stop",
                    "error",
                    str(e),
                    {
                        "checkpoint_preserved": True,
                        "worker_attempt_consumed": False,
                        "operator_action_required": True,
                    },
                )
            except Exception:
                pass
            lf.write(f"\n[LLM_AVAILABILITY_STATE_INVALID] {e}\n")
            return ORCH_LLM_AVAILABILITY_BLOCKED_COST

        except Exception as e:
            # Every stream exit runs the single owned cleanup in
            # _stream_response.finally. Cleanup failures arrive here as typed
            # ConnectionError infrastructure failures and are never suppressed.
            cycle_failed = True
            # P2: classify infra (SDK signature/timeout/connection) vs real business failure.
            # is_llm_infra_error is type-based (ClaudeSDKError, asyncio.TimeoutError,
            # ConnectionError, OSError) — same classifier used by tool_gates/agent_review
            # for critic/reviewer infra short-circuit (commit 5c14d01). Keyword fallback
            # remains for defense-in-depth (older SDK error formats).
            is_shutdown_cancel = (
                shutdown_mgr is not None
                and shutdown_mgr.is_shutting_down
                and _is_shutdown_cancel_error(e)
            )
            is_infra = (
                not is_shutdown_cancel
                and (
                    isinstance(e, (_OrchFirstActivityTimeout, _OrchActionableStageTimeout, _OrchStreamStallTimeout))
                    or _is_cycle_infra_error(e)
                )
            )
            # SDK streaming errors (missing 'signature' field on thinking blocks,
            # observed with claude_agent_sdk 0.2.91 + adaptive thinking) leave the
            # provider stream broken — a fresh stream avoids replaying the same crash (the v84
            # quality_passed→run_review infinite-retry deadlock). P0 disabled
            # thinking so this no longer fires, but the guard prevents silent
            # recurrence if thinking is ever re-enabled or another SDK stream
            # error appears.
            if is_shutdown_cancel:
                shutdown_cancelled = True
                cycle_failed = False
                if ui:
                    ui.log_history(
                        "[Orchestrator] Claude stream stopped during shutdown; checkpoint preserved for a fresh provider stream.",
                        "warn",
                    )
                else:
                    log.warning("Claude stream stopped during shutdown; checkpoint preserved: %s", e)
                try:
                    log_system_event(
                        "orchestrator.shutdown_cancelled",
                        "info",
                        "Claude stream stopped during orchestrator shutdown; checkpoint preserved and provider history discarded",
                        {"exception_type": type(e).__name__, "error": str(e)[:500]},
                    )
                except Exception:
                    pass
            elif is_infra:
                infra_error = True  # ALSO set here — signature path must trigger -0.5 sentinel, not -1.0
                _clear_orchestrator_session()
                if ui:
                    ui.log_history(
                        f"[Orchestrator] LLM infrastructure error ({type(e).__name__}, NOT auth) — "
                        "provider history discarded; next cycle will use a fresh checkpoint-bound stream.", "warn",
                    )
                else:
                    log.warning(
                        "LLM infra error (%s); fresh checkpoint-bound provider retry will be attempted: %s",
                        type(e).__name__, e,
                    )
                try:
                    log_system_event("pipeline.sdk_stream_error", "warn",
                        f"Orchestrator LLM infra error ({type(e).__name__}): {e}",
                        {"provider_history_discarded": True, "exception_type": type(e).__name__})
                except Exception:
                    pass
            else:
                # Checkpoint/transaction state remains available for recovery.
                if ui:
                    ui.log_history(f"[Orchestrator] Error: {e}", "error")
                else:
                    log.error("Error: %s", e)
            lf.write(f"\n[{'SHUTDOWN_CANCELLED' if is_shutdown_cancel else 'ERROR'}] {e}\n")

    # Remove any legacy sidecar after natural completion.  Provider session IDs
    # are never persisted on either success or failure.
    if cycle_completed:
        _clear_orchestrator_session()

    # Return negative cost to signal auth error for fast backoff.
    # P2: auth_error returns must be STRICTLY < -1.0 so orchestrator_loop's
    # `auth_error = cost < -1.0` inference distinguishes auth from generic crash (-1.0).
    # root-cause-audit bug-check: 旧 -max(abs,1.0) 在 total_cost≤1 时 = -1.0 被误判 generic
    # (低成本 auth fail 最常见——子 agent 没跑就 401/403)。统一 -0.5 偏移保证 auth 总是 < -1.0
    # (infra=-0.5 / generic=-1.0 / auth≤-1.5 三者互斥)。
    if auth_error:
        return -max(abs(total_cost), 1.0) - 0.5

    if shutdown_cancelled:
        return SHUTDOWN_CANCEL_COST

    # P1: a crashed cycle must NOT return partial cost > 0. orchestrator_loop's
    # `if cost >= 0` branch would treat it as success, run cleanup, and log
    # "gen complete" — masking the failure. This was the v84 deadlock amplifier:
    # each signature-error crash was recorded as a $4.13/$0.00 "complete" gen
    # while pipeline_state never advanced past quality_passed. Return -1.0 (or
    # -0.5 for infra errors) so the loop retries instead of pretending success.
    # P2: -0.5 for infra (SDK signature/timeout/connection) — orchestrator_loop's
    # cost<0 branch routes -0.5 to short backoff (15s) instead of misclassifying
    # as auth error (300s). -0.5 is mathematically distinct from -1.0 and
    # -max(abs(cost), 1.0) (auth clamp ≥1.0). Session already cleared above for
    # infra errors; for other exceptions the loop's cost==-1.0 branch clears it.
    if cycle_failed:
        return -0.5 if infra_error else -1.0

    # On non-happy paths (KeyboardInterrupt — explicit user interrupt), total_cost
    # may only be the Orchestrator's partial session cost. Return the full tracked
    # cost delta when UI is available.
    if ui and not cycle_completed:
        return ui.gen_cost_total - _cost_at_start

    return total_cost


def _checkpoint_recovery_context(reason: str, ui=None, *, log_level: str = "warn", label: str = "[Recovery]"):
    """Build a recovery context from an active pipeline checkpoint.

    LLM sessions are disposable after SDK/cost-cap failures; pipeline checkpoints
    are not. This helper keeps those concepts separate so an infra retry resumes
    the same generation instead of falling back to Phase 1 source selection.
    """
    observation = _pipeline_checkpoint_observation()
    checkpoint = observation.get("checkpoint")
    checkpoint_error = observation.get("error")
    if checkpoint_error:
        msg = (
            f"{label} Checkpoint authority is unreadable or invalid after "
            f"{reason}: {checkpoint_error}."
        )
        if ui:
            ui.log_history(msg, "error")
        else:
            log.error(msg)
        return {
            "action": "blocked",
            "reason": "checkpoint_unreadable_or_invalid",
            "checkpoint": None,
            "diagnostics": {
                "active": True,
                "recoverable": False,
                "issues": [str(checkpoint_error)],
                "checkpoint_path_exists": observation.get("path_exists"),
            },
        }

    if not checkpoint:
        try:
            from post_publication_handoff import (
                pending_handoff_route,
                pending_handoff_route_checkpoint,
            )

            handoff = pending_handoff_route()
        except Exception as exc:
            handoff = {
                "status": "blocked",
                "issues": [f"handoff_discovery_failed:{type(exc).__name__}"],
            }
        if handoff.get("status") == "blocked":
            return {
                "action": "blocked",
                "reason": "post_publication_handoff_ambiguous_or_invalid",
                "checkpoint": None,
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": list(handoff.get("issues") or []),
                    "post_publication_handoff": True,
                },
            }
        if handoff.get("status") != "pending":
            return None
        route_checkpoint = pending_handoff_route_checkpoint(handoff)
        msg = (
            f"{label} Resuming published v{handoff['version']} at the durable "
            f"post-publication Archivist handoff after {reason}."
        )
        if ui:
            ui.log_history(msg, log_level)
        elif log_level == "info":
            log.info(msg)
        else:
            log.warning(msg)
        return {
            "action": "resume",
            "checkpoint": route_checkpoint,
            "session_id": None,
            "stage": "post_publication_handoff",
            "next_v": handoff["version"],
            "source_v": handoff["source_v"],
            "post_publication_handoff": True,
            "log_level": log_level,
            "label": label,
        }

    stage = checkpoint.get("stage")
    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    if next_v is None or source_v is None:
        return None
    try:
        from pipeline_recovery import checkpoint_recovery_diagnostics
        recovery_diag = checkpoint_recovery_diagnostics(checkpoint)
    except Exception as e:
        recovery_diag = {
            "active": True,
            "recoverable": False,
            "issues": ["checkpoint_recovery_diagnostic_failed"],
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }
    if stage == "official_bootstrap_required":
        issues = list(recovery_diag.get("issues") or [])
        expected_issue = "official_bootstrap_requires_operator_action"
        unexpected = [issue for issue in issues if issue != expected_issue]
        if (
            recovery_diag.get("active") is True
            and expected_issue in issues
            and not unexpected
        ):
            return {
                "action": "operator_action_required",
                "reason": expected_issue,
                "checkpoint": checkpoint,
                "stage": stage,
                "next_v": next_v,
                "source_v": source_v,
                "operator_action_required": True,
                "diagnostics": recovery_diag,
            }
    if recovery_diag.get("active") and not recovery_diag.get("recoverable"):
        issues = list(recovery_diag.get("issues") or [])
        msg = (
            f"[Recovery] Refusing checkpoint resume for v{next_v} at '{stage}' "
            f"after {reason}: {', '.join(issues)}."
        )
        if ui:
            ui.log_history(msg, "error")
        else:
            log.error(msg)
        try:
            log_system_event(
                "orchestrator.recovery_blocked",
                "error",
                msg,
                {
                    "case": f"blocked_after_{reason}",
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "issues": issues,
                    "diagnostics": recovery_diag,
                },
            )
        except Exception:
            pass
        return {
            "action": "blocked",
            "reason": "unrecoverable_checkpoint",
            "checkpoint": checkpoint,
            "diagnostics": recovery_diag,
        }

    dead_stages = {None, "archived", "abandoned"}
    if stage in dead_stages:
        return None

    recovery = {
        "action": "resume",
        "checkpoint": checkpoint,
        "session_id": None,  # force a fresh LLM session, but keep pipeline identity
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
        "log_level": log_level,
        "label": label,
    }

    msg = f"{label} Resuming v{next_v} at '{stage}' after {reason} (new LLM session)."
    if ui:
        ui.log_history(msg, log_level)
    else:
        if log_level == "info":
            log.info(msg)
        else:
            log.warning(msg)
    try:
        log_system_event(
            "orchestrator.recovery_decision", log_level, msg,
            {"case": f"resume_after_{reason}",
             "next_v": next_v, "source_v": source_v,
             "stage": stage, "session_present": False},
        )
    except Exception:
        pass
    return recovery


def _recovery_route_log_kwargs(recovery):
    """Return deterministic-route log metadata carried by a recovery context."""
    if not isinstance(recovery, dict):
        return {}
    return {
        "log_level": recovery.get("log_level") or "warn",
        "label": recovery.get("label") or "[Recovery]",
    }


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
        # Provider/Worker text is diagnostic only and must never select a
        # control-plane abandon capability.
        return "worker_workflow_abandoned"
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


async def _try_deterministic_checkpoint_route(
    recovery,
    ui=None,
    *,
    log_level: str = "warn",
    label: str = "[Recovery]",
    cost_policy: GenerationCostPolicy | None = None,
    shutdown_mgr=None,
    outcome: dict | None = None,
):
    """Execute safe checkpoint routes without asking the Orchestrator LLM again."""
    if not recovery or recovery.get("action") != "resume":
        return False
    checkpoint = recovery.get("checkpoint") or {}
    _bind_generation_cost_runtime(
        checkpoint,
        ui=ui,
        policy=cost_policy,
    )
    _check_generation_cost_policy(ui)
    route = _resolve_recovery_route(checkpoint)
    if not route:
        return False

    next_tool = route.get("next_tool")
    next_v = route.get("next_v")
    source_v = route.get("source_v")
    parent2_v = route.get("parent2_v")
    stage = route.get("stage")

    if _deterministic_route_requires_llm(checkpoint, str(next_tool)):
        if not await _honor_active_llm_pause(ui, shutdown_mgr):
            return False

    saved_session_id = _load_orchestrator_session()
    if saved_session_id:
        session_clear_reason = (
            "deterministic_master_planned_route"
            if stage == "master_planned"
            else f"deterministic_{next_tool}_route"
        )
        _clear_orchestrator_session(reason=session_clear_reason)

    if next_v is None or source_v is None:
        return False

    if next_tool == "run_crossover":
        try:
            parent2_v = int(parent2_v) if parent2_v is not None else None
        except (TypeError, ValueError):
            parent2_v = None
        if parent2_v is None:
            return False

    try:
        handler, args = _deterministic_route_handler_and_args(
            next_tool,
            checkpoint,
            next_v,
            source_v,
            parent2_v,
        )
    except Exception:
        handler, args = None, None

    if not callable(handler):
        return False

    msg = (
        f"{label} Deterministically routing v{next_v} at {stage} "
        f"to {next_tool}."
    )
    if ui:
        ui.log_history(msg, log_level)
    else:
        if log_level == "info":
            log.info(msg)
        else:
            log.warning(msg)
    try:
        log_system_event(
            f"pipeline.deterministic_route_{next_tool}",
            log_level,
            msg,
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": stage,
                "parent2_v": checkpoint.get("parent2_v"),
                "route": route,
            },
        )
    except Exception:
        pass

    try:
        if recovery.get("post_publication_handoff") is True:
            from tool_runtime_guard import system_deterministic_route_authority

            with system_deterministic_route_authority(next_tool, checkpoint):
                result = await handler(args)
        else:
            result = await handler(args)
        data = _extract_tool_result_json(result)
        if outcome is not None:
            outcome.clear()
            outcome.update({
                "checkpoint": checkpoint,
                "route": route,
                "result": data,
                "terminal_abandon_result": _completed_abandon_tool_result(data),
            })
        # Direct checkpoint recovery bypasses the SDK ToolResult stream where
        # this conversion normally happens.  Re-establish the same typed
        # availability boundary before generic tool-error routing can retry an
        # Orchestrator/Worker or consume an infrastructure attempt.
        _raise_for_llm_availability_tool_result(data)
    except LLMAvailabilityBlocked as exc:
        # Direct deterministic routes do not pass through _run_one_cycle's SDK
        # stream catch. Persist the same durable control state here and leave the
        # checkpoint/attempt untouched.
        try:
            persist_llm_pause(exc)
        except Exception as pause_exc:
            if ui:
                ui.log_history(
                    f"[Recovery] LLM pause persistence failed closed: {pause_exc}",
                    "error",
                )
                ui.set_status(
                    "Stopped: LLM pause persistence failed",
                    is_working=False,
                )
            log.error(
                "Deterministic route could not persist LLM pause: %s",
                pause_exc,
            )
            raise LLMAvailabilityPauseError(
                "deterministic route could not persist the classified LLM pause"
            ) from pause_exc
        _clear_orchestrator_session(reason="deterministic_llm_availability_blocked")
        if await _honor_active_llm_pause(ui, shutdown_mgr):
            return True
        return False
    except LLMAvailabilityPauseError as exc:
        if ui:
            ui.log_history(f"[Recovery] LLM pause control failed closed: {exc}", "error")
            ui.set_status("Stopped: LLM pause control invalid", is_working=False)
        log.error("Deterministic route LLM pause control failed closed: %s", exc)
        raise
    _check_generation_cost_policy(ui)
    error = data.get("error")
    success = data.get("success")
    worker_terminal_abandon = (
        next_tool == "execute_workers"
        and _is_worker_terminal_abandon_result(data)
    )
    if data.get("pending") and data.get("action") == "poll_commit_bot":
        wait_sec = max(5.0, min(60.0, float(data.get("retry_after_sec", 30) or 30)))
        try:
            log_system_event(
                "pipeline.deterministic_route_pending",
                "info",
                f"Durable official certification remains pending for v{next_v}; polling in {wait_sec:g}s",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "next_tool": next_tool,
                    "retry_after_sec": wait_sec,
                },
            )
        except Exception:
            pass
        await asyncio.sleep(wait_sec)
        return True
    if next_tool == "run_master" and _is_master_ensemble_pending_retry(
        data,
        checkpoint,
    ):
        wait_sec = max(
            5.0,
            min(60.0, float(data.get("retry_after_sec", 5) or 5)),
        )
        try:
            log_system_event(
                "pipeline.deterministic_master_role_retry_pending",
                "warn" if not data.get("needs_attention") else "error",
                (
                    f"Journaled Master role {data.get('slot')} remains pending "
                    f"for v{next_v}; retrying in {wait_sec:g}s"
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "slot": data.get("slot"),
                    "role_attempt": data.get("role_attempt"),
                    "accepted_slots": data.get("accepted_slots"),
                    "pending_slots": data.get("pending_slots"),
                    "retry_after_sec": wait_sec,
                    "needs_attention": bool(data.get("needs_attention")),
                },
            )
        except Exception:
            pass
        await asyncio.sleep(wait_sec)
        return True
    if (
        next_tool == "execute_workers"
        and _is_worker_operator_shutdown_interrupted(data, checkpoint)
        and shutdown_mgr is not None
        and shutdown_mgr.is_shutting_down
    ):
        try:
            log_system_event(
                "pipeline.worker_operator_shutdown_interrupted",
                "info",
                (
                    f"Worker lease for v{next_v} was fenced attempt-neutrally "
                    "during operator shutdown"
                ),
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "workflow_run_id": data.get("workflow_run_id"),
                    "effect_id": data.get("effect_id"),
                    "lease_epoch": data.get("lease_epoch"),
                    "claimed_attempt": data.get("claimed_attempt"),
                    "restored_attempt": data.get("restored_attempt"),
                    "checkpoint_preserved": True,
                },
            )
        except Exception:
            pass
        if ui:
            ui.log_history(
                "[Recovery] Worker stopped at the operator shutdown edge; "
                "the same frozen activity will be reclaimed after restart.",
                "info",
            )
        return False
    if error or worker_terminal_abandon:
        if next_tool == "execute_workers" and (
            _is_worker_circuit_breaker_result(data)
            or _is_precommit_rework_circuit_breaker_result(data)
            or _is_official_rework_circuit_breaker_result(data)
            or worker_terminal_abandon
        ):
            if _is_precommit_rework_circuit_breaker_result(data):
                abandon_reason = "precommit_rework_circuit_breaker"
            elif _is_official_rework_circuit_breaker_result(data):
                abandon_reason = "official_rework_circuit_breaker"
            elif _is_worker_circuit_breaker_result(data):
                abandon_reason = "worker_circuit_breaker"
            else:
                abandon_reason = _worker_terminal_abandon_reason(data)
            if (
                _is_official_rework_circuit_breaker_result(data)
                and data.get("abandoned") is True
            ):
                abandon_result = data.get("abandon_result") or {
                    "abandoned": True,
                    "reason": abandon_reason,
                }
                abandoned = True
            else:
                from evolution_core import read_pipeline_checkpoint
                from tool_bot_management import (
                    _do_abandon_generation,
                    expected_abandon_identity,
                )
                abandon_result = await _do_abandon_generation(
                    reason=abandon_reason,
                    **expected_abandon_identity(read_pipeline_checkpoint()),
                )
                abandoned = bool(abandon_result.get("abandoned"))
            if outcome is not None:
                outcome["router_abandon_result"] = abandon_result
                outcome["terminal_abandon_result"] = (
                    _completed_abandon_tool_result(abandon_result)
                )
            msg_abandon = (
                f"{abandon_reason} reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "error" if not abandoned else "warn")
            else:
                log.warning(msg_abandon)
            try:
                log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                        "abandon_result": abandon_result,
                    },
                )
            except Exception:
                pass
            return abandoned

        if next_tool == "run_crossover" and _is_crossover_incompatible_result(data):
            abandoned = bool(data.get("abandoned"))
            msg_abandon = (
                f"crossover_incompatible reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "warn" if abandoned else "error")
            else:
                log.warning(msg_abandon) if abandoned else log.error(msg_abandon)
            try:
                log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                    },
                )
            except Exception:
                pass
            return abandoned

        if next_tool == "run_crossover" and _is_crossover_llm_exhausted_result(data):
            abandoned = bool(data.get("abandoned"))
            msg_abandon = (
                f"crossover_llm_exhausted reached for v{next_v}; "
                f"{'abandoned generation' if abandoned else 'abandon did not complete'}."
            )
            if ui:
                ui.log_history(f"[Recovery] {msg_abandon}", "warn" if abandoned else "error")
            else:
                log.warning(msg_abandon) if abandoned else log.error(msg_abandon)
            try:
                log_system_event(
                    "pipeline.deterministic_route_abandoned",
                    "warn" if abandoned else "error",
                    msg_abandon,
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "stage": stage,
                        "result": data,
                    },
                )
            except Exception:
                pass
            return abandoned

        detail = f"Deterministic {next_tool} route failed for v{next_v}: {str(error)[:180]}"
        if ui:
            ui.log_history(f"[Recovery] {detail}", "error")
        else:
            log.error(detail)
        try:
            log_system_event(
                "pipeline.deterministic_route_failed",
                "error",
                detail,
                {"next_v": next_v, "source_v": source_v, "stage": stage, "result": data},
            )
        except Exception:
            pass
        return False

    try:
        route_succeeded = success is not False
        log_system_event(
            "pipeline.deterministic_route_done",
            "success" if route_succeeded else "warn",
            f"Deterministic {next_tool} route completed for v{next_v}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "stage": stage,
                "success": route_succeeded,
                "reported_success": success,
            },
        )
    except Exception:
        pass
    return True


def _classify_recovery_after_deterministic_route(
    recovery,
    outcome,
    next_recovery,
):
    """Prove why a deterministic route no longer has a live checkpoint."""

    result = (outcome or {}).get("result")
    if isinstance(result, dict) and result.get("recovery_blocked") is True:
        issues = list(result.get("validation_errors") or [])
        return {
            "action": "blocked",
            "reason": str(result.get("error") or "deterministic_authority_blocked"),
            "checkpoint": (recovery or {}).get("checkpoint"),
            "diagnostics": {
                "active": True,
                "recoverable": False,
                "issues": issues or ["deterministic_authority_blocked"],
                "failure_class": result.get("failure_class"),
                "operator_action": result.get("action"),
            },
        }
    if next_recovery is not None:
        return next_recovery
    checkpoint = (recovery or {}).get("checkpoint") or {}
    if (recovery or {}).get("post_publication_handoff") is True:
        return {
            "action": "publication_handoff_completed",
            "checkpoint": checkpoint,
        }
    terminal_result = (outcome or {}).get("terminal_abandon_result")
    if terminal_result is not None:
        try:
            from tool_bot_management import validate_completed_abandon_handoff

            proof = validate_completed_abandon_handoff(
                checkpoint,
                terminal_result,
            )
        except Exception as exc:
            return {
                "action": "blocked",
                "reason": "deterministic_terminal_proof_invalid",
                "checkpoint": None,
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "deterministic_terminal_proof_invalid:"
                        f"{str(exc)[:240]}"
                    ],
                },
            }
        return {
            "action": "generation_abandoned",
            "checkpoint": None,
            "terminal_proof": proof,
        }
    return {
        "action": "blocked",
        "reason": "deterministic_checkpoint_disappeared_without_proof",
        "checkpoint": None,
        "diagnostics": {
            "active": True,
            "recoverable": False,
            "issues": [
                "deterministic_checkpoint_disappeared_without_proof"
            ],
        },
    }


async def _advance_deterministic_recovery(
    recovery,
    ui,
    *,
    cost_policy,
    shutdown_mgr,
    log_level="info",
    label="[Pipeline]",
    gen_ctx=None,
    gen_count=None,
):
    """Run one deterministic route and classify every checkpoint-free result."""

    outcome = {}
    routed = await _try_deterministic_checkpoint_route(
        recovery,
        ui,
        log_level=log_level,
        label=label,
        cost_policy=cost_policy,
        shutdown_mgr=shutdown_mgr,
        outcome=outcome,
    )
    if not routed and not outcome:
        return {
            "routed": False,
            "recovery": recovery,
            "outcome": outcome,
            "terminal_action": None,
        }
    next_recovery = _checkpoint_recovery_context(
        "deterministic_route",
        ui,
        log_level=log_level,
        label=label,
    )
    result = outcome.get("result") if isinstance(outcome, dict) else None
    shutdown_edge = bool(
        shutdown_mgr is not None and shutdown_mgr.is_shutting_down
    )
    if (
        shutdown_edge
        and isinstance(result, dict)
        and result.get("error") == "WORKER_OPERATOR_SHUTDOWN_INTERRUPTED"
    ):
        outcome_route = outcome.get("route") if isinstance(outcome, dict) else None
        route_is_worker = bool(
            isinstance(outcome_route, dict)
            and outcome_route.get("next_tool") == "execute_workers"
        )
        if (
            route_is_worker
            and _is_worker_operator_shutdown_interrupted(
                result,
                (recovery or {}).get("checkpoint") or {},
            )
        ):
            # This is a routed process-lifecycle boundary, not an invitation
            # to fall through to a new Orchestrator provider stream. Keep the
            # exact checkpoint recovery for the next process; both continuous
            # and one-gen drivers observe routed=True, loop once, and stop on
            # their shutdown edge before any provider dispatch.
            return {
                "routed": True,
                "recovery": next_recovery or recovery,
                "outcome": outcome,
                "terminal_action": "operator_shutdown_interrupted",
                "terminal_proof": None,
            }
        return {
            "routed": True,
            "recovery": {
                "action": "blocked",
                "reason": "worker_operator_shutdown_projection_invalid",
                "checkpoint": (recovery or {}).get("checkpoint"),
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "worker_operator_shutdown_projection_invalid"
                    ],
                },
            },
            "outcome": outcome,
            "terminal_action": "operator_shutdown_projection_invalid",
            "terminal_proof": None,
        }
    if (
        not routed
        and next_recovery is not None
        and next_recovery.get("action") == "resume"
        and not (
            isinstance(outcome.get("result"), dict)
            and outcome["result"].get("recovery_blocked") is True
        )
    ):
        return {
            "routed": False,
            "recovery": next_recovery,
            "outcome": outcome,
            "terminal_action": None,
        }
    classified = _classify_recovery_after_deterministic_route(
        recovery,
        outcome,
        next_recovery,
    )
    action = classified.get("action")
    terminal_action = action
    terminal_proof = None
    if action == "publication_handoff_completed":
        cleanup_ctx = gen_ctx or _generation_context_from_checkpoint(
            (recovery or {}).get("checkpoint") or {},
            gen_count=gen_count or 1,
        )
        cleanup_ok = await _run_post_generation_cleanup_with_timeout(
            shutdown_mgr,
            ui,
            cleanup_ctx,
            gen_count=gen_count,
        )
        if cleanup_ok is True:
            classified = None
        else:
            classified = {
                "action": "blocked",
                "reason": "post_generation_cleanup_verification_failed",
                "checkpoint": None,
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": [
                        "post_generation_cleanup_verification_failed"
                    ],
                },
            }
            terminal_action = "post_generation_cleanup_failed"
    elif action == "generation_abandoned":
        terminal_proof = classified.get("terminal_proof")
        try:
            log_system_event(
                "orchestrator.deterministic_generation_abandoned",
                "warn",
                "Deterministic route reached a verified abandon boundary",
                terminal_proof or {},
            )
        except Exception:
            pass
        classified = None
    return {
        "routed": True,
        "recovery": classified,
        "outcome": outcome,
        "terminal_action": terminal_action,
        "terminal_proof": terminal_proof,
    }


async def _run_post_generation_cleanup_with_timeout(shutdown_mgr, ui, gen_ctx, gen_count=None):
    """Run post-generation housekeeping without letting it block evolution forever."""
    from generation_scheduler import post_generation_cleanup

    version = getattr(gen_ctx, "next_v", None)
    source_v = getattr(gen_ctx, "source_v", None)
    started = time.time()
    log_system_event(
        "orchestrator.post_cleanup_start",
        "info",
        f"Post-generation cleanup starting for v{version}",
        {
            "version": version,
            "source_v": source_v,
            "gen_count": gen_count,
            "timeout_s": POST_GENERATION_CLEANUP_TIMEOUT,
        },
    )
    try:
        await asyncio.wait_for(
            post_generation_cleanup(shutdown_mgr, ui, gen_ctx),
            timeout=POST_GENERATION_CLEANUP_TIMEOUT,
        )
    except asyncio.TimeoutError:
        elapsed = time.time() - started
        msg = (
            f"Post-generation cleanup timed out for v{version} after "
            f"{POST_GENERATION_CLEANUP_TIMEOUT}s; stopping before successor "
            "scheduling because the checkpoint-free boundary remains blocked."
        )
        log.warning(msg)
        if ui:
            ui.log_history(msg, "warn")
        log_system_event(
            "orchestrator.post_cleanup_timeout",
            "warn",
            msg,
            {
                "version": version,
                "source_v": source_v,
                "gen_count": gen_count,
                "elapsed_sec": round(elapsed, 2),
                "timeout_s": POST_GENERATION_CLEANUP_TIMEOUT,
            },
        )
        return False
    except OperatorGenerationCostLimitExceeded:
        # Archivist/consolidation calls are part of the same generation.  Do not
        # translate an operator stop into best-effort cleanup and then start a
        # fresh generation with a reset scope.
        raise
    except Exception as e:
        elapsed = time.time() - started
        msg = f"Post-generation cleanup failed for v{version}: {str(e)[:180]}"
        log.exception(msg)
        if ui:
            ui.log_history(msg, "warn")
        log_system_event(
            "orchestrator.post_cleanup_failed",
            "error",
            msg,
            {
                "version": version,
                "source_v": source_v,
                "gen_count": gen_count,
                "elapsed_sec": round(elapsed, 2),
                "error": str(e)[:500],
            },
        )
        return False

    elapsed = time.time() - started
    log_system_event(
        "orchestrator.post_cleanup_done",
        "info",
        f"Post-generation cleanup finished for v{version} in {elapsed:.1f}s",
        {
            "version": version,
            "source_v": source_v,
            "gen_count": gen_count,
            "elapsed_sec": round(elapsed, 2),
        },
    )
    return True


async def _watchdog_coroutine(ui, shutdown_mgr, check_interval=60):
    """Background coroutine that monitors pipeline_state.json for stuck stages.

    Every `check_interval` seconds, reads the pipeline checkpoint and checks
    `last_stage_change_ts`. If more than WATCHDOG_TIMEOUT seconds have elapsed
    with no stage change, clears the orchestrator session and sets the
    _watchdog_triggered flag so the main loop will restart from the checkpoint.

    Only triggers when:
      - this process owns an active orchestrator provider stream
      - The checkpoint stage is in the recoverable set
      - No stage change for > WATCHDOG_TIMEOUT seconds
    """
    global _watchdog_triggered
    from evolution_infra import WATCHDOG_TIMEOUT
    from evolution_core import read_pipeline_checkpoint
    from pipeline_state import (
        pipeline_runtime_activity_ts,
        session_recoverable_stages,
    )

    recoverable_stages = session_recoverable_stages()

    while True:
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return
        try:
            await asyncio.sleep(check_interval)
            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                return

            # Provider session IDs are never persisted.  The in-process owned
            # stream flag supplies liveness without granting history authority.
            if not _orchestrator_provider_stream_active:
                continue

            checkpoint = read_pipeline_checkpoint()
            if not checkpoint:
                continue

            stage = checkpoint.get("stage", "unknown")
            if stage not in recoverable_stages:
                continue

            last_ts = max(
                float(checkpoint.get("last_stage_change_ts") or 0.0),
                pipeline_runtime_activity_ts(checkpoint),
            )
            if last_ts <= 0:
                continue

            elapsed = time.time() - last_ts
            if elapsed > WATCHDOG_TIMEOUT:
                next_v = checkpoint.get("next_v", "?")
                msg = (f"[Watchdog] Pipeline stuck at '{stage}' for v{next_v} "
                       f"({elapsed:.0f}s > {WATCHDOG_TIMEOUT}s). "
                       f"Clearing session to force restart.")
                if ui:
                    ui.log_history(msg, "warn")
                else:
                    log.warning(msg)
                log_system_event("pipeline.watchdog_recovery", "warn",
                                 "Watchdog triggered: clearing stale orchestrator session",
                                 {"next_v": next_v, "stage": stage,
                                  "elapsed_s": round(elapsed, 1),
                                  "watchdog_timeout": WATCHDOG_TIMEOUT})
                _clear_orchestrator_session()
                _watchdog_triggered = True
                # Exit — the main loop will detect the flag and restart
                return
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.debug("Watchdog check error (non-fatal): %s", e)


def _runtime_branch_guard_enabled() -> bool:
    if os.environ.get("POK_DISABLE_RUNTIME_BRANCH_GUARD") == "1":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("POK_FORCE_RUNTIME_BRANCH_GUARD") != "1":
        return False
    return True


def _branch_name(branch_status: str | None) -> str:
    parts = (branch_status or "").split("...", 1)[0].split()
    return parts[0] if parts else ""


def _runtime_git_identity() -> dict:
    """Read the current branch and HEAD without mutating the worktree."""
    branch_status = ""
    head = ""
    try:
        status = subprocess.run(
            ["git", "status", "--short", "--branch", "--untracked-files=no"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.returncode == 0:
            lines = [line for line in (status.stdout or "").splitlines() if line.strip()]
            if lines and lines[0].startswith("## "):
                branch_status = lines[0].replace("## ", "", 1)
    except Exception:
        branch_status = ""
    if not branch_status:
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if branch.returncode == 0:
                branch_status = (branch.stdout or "").strip()
        except Exception:
            branch_status = ""
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if rev.returncode == 0:
            head = (rev.stdout or "").strip()
    except Exception:
        head = ""
    return {
        "branch": _branch_name(branch_status),
        "branch_status": branch_status,
        "head": head,
    }


def _runtime_head_drift_unrelated_allowed(expected_head: str, current_head: str) -> tuple[bool, dict]:
    if not expected_head or not current_head or expected_head == current_head:
        return False, {}
    try:
        from evolution_core import read_pipeline_checkpoint
        checkpoint = read_pipeline_checkpoint()
    except Exception:
        checkpoint = None
    candidate_v = None
    if isinstance(checkpoint, dict):
        try:
            candidate_v = int(checkpoint.get("next_v"))
        except Exception:
            candidate_v = None
    allowed, payload = evaluate_head_drift(
        PROJECT_ROOT,
        expected_head,
        current_head,
        candidate_v=candidate_v,
        checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
    )
    contract_paths = list(payload.get("head_contract_paths") or [])
    candidate_prefix = bot_relpath(candidate_v) + "/" if candidate_v is not None else ""
    payload.update({
        "candidate_v": candidate_v,
        "head_candidate_entries": [
            f"?? {path}" for path in contract_paths
            if candidate_prefix and path.startswith(candidate_prefix)
        ][:40],
        "head_blocking_entries": [
            f"?? {path}" for path in contract_paths
            if not candidate_prefix or not path.startswith(candidate_prefix)
        ][:40],
    })
    return allowed, payload


def _set_runtime_expected_head(head: str) -> str:
    """Publish the current safe runtime HEAD for tool-level guards."""
    clean_head = (head or "").strip()
    if clean_head:
        os.environ["POK_RUNTIME_EXPECTED_HEAD"] = clean_head
    else:
        os.environ.pop("POK_RUNTIME_EXPECTED_HEAD", None)
    return clean_head


async def _runtime_branch_guard_coroutine(
    ui,
    shutdown_mgr,
    *,
    expected_branch: str,
    expected_head: str,
    owner_task=None,
    hard_stop_event=None,
    check_interval: float = RUNTIME_BRANCH_GUARD_INTERVAL,
):
    """Stop in-place evolution if another actor changes this worktree's branch.

    Dirty-path scope can be made safe, but git branch/HEAD is global to a
    worktree. If another agent switches or advances HEAD while workers are
    running, the LLM may read a different codebase than the one that passed
    gates. A branch-name change to the same HEAD is only an alias of the same
    tree, so it is recorded and tolerated until a commit/HEAD change appears.
    """
    allowed_aliases: set[tuple[str, str]] = set()
    allowed_unrelated_heads: set[tuple[str, str, str]] = set()
    runtime_expected_head = _set_runtime_expected_head(expected_head)
    while True:
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return
        try:
            await asyncio.sleep(check_interval)
            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                return
            current = _runtime_git_identity()
            current_branch = current.get("branch") or ""
            current_head = current.get("head") or ""
            reason = ""
            published_expected_head = os.environ.get("POK_RUNTIME_EXPECTED_HEAD", "").strip()
            if (
                published_expected_head
                and published_expected_head != runtime_expected_head
                and current_head == published_expected_head
            ):
                previous_expected_head = runtime_expected_head
                runtime_expected_head = published_expected_head
                log_system_event(
                    "repo.runtime_expected_head_adopted",
                    "info",
                    (
                        "Runtime branch guard adopted published expected HEAD: "
                        f"{previous_expected_head or '<none>'} -> {runtime_expected_head}"
                    ),
                    {
                        "expected_branch": expected_branch,
                        "current_branch": current_branch,
                        "previous_expected_head": previous_expected_head,
                        "expected_head": runtime_expected_head,
                        "current_head": current_head,
                        "branch_status": current.get("branch_status", ""),
                        "directive": (
                            "Continuing because a pipeline-owned operation "
                            "published the current HEAD as the validated runtime baseline."
                        ),
                    },
                )
            same_expected_head = bool(
                runtime_expected_head
                and current_head
                and current_head == runtime_expected_head
            )
            branch_alias = bool(
                expected_branch
                and current_branch
                and current_branch != expected_branch
                and same_expected_head
            )
            if branch_alias:
                alias_key = (current_branch, current_head)
                if alias_key not in allowed_aliases:
                    allowed_aliases.add(alias_key)
                    log_system_event(
                        "repo.runtime_branch_alias_allowed",
                        "warn",
                        (
                            "Runtime branch guard tolerated branch alias on the "
                            f"same HEAD: {expected_branch}@{runtime_expected_head} -> "
                            f"{current_branch}@{current_head}"
                        ),
                        {
                            "expected_branch": expected_branch,
                            "current_branch": current_branch,
                            "expected_head": runtime_expected_head,
                            "current_head": current_head,
                            "branch_status": current.get("branch_status", ""),
                            "directive": (
                                "Continuing because the worktree HEAD is unchanged. "
                                "A later evaluation-contract HEAD change will stop evolution; commit_bot "
                                "still requires the canonical branch."
                            ),
                        },
                    )
                continue
            if runtime_expected_head and current_head and current_head != runtime_expected_head:
                unrelated_allowed, unrelated_payload = _runtime_head_drift_unrelated_allowed(
                    runtime_expected_head,
                    current_head,
                )
                if unrelated_allowed:
                    previous_expected_head = runtime_expected_head
                    runtime_expected_head = _set_runtime_expected_head(current_head)
                    drift_key = (current_branch, previous_expected_head, current_head)
                    if drift_key not in allowed_unrelated_heads:
                        allowed_unrelated_heads.add(drift_key)
                        log_system_event(
                            "repo.runtime_head_drift_unrelated_allowed",
                            "warn",
                            (
                                "Runtime branch guard tolerated unrelated HEAD drift: "
                                f"{expected_branch}@{previous_expected_head} -> "
                                f"{current_branch}@{current_head}"
                            ),
                            {
                                "expected_branch": expected_branch,
                                "current_branch": current_branch,
                                "expected_head": previous_expected_head,
                                "current_head": current_head,
                                "advanced_expected_head": runtime_expected_head,
                                "branch_status": current.get("branch_status", ""),
                                **unrelated_payload,
                                "directive": (
                                    "Continuing because the HEAD change does not touch "
                                    "evolution infrastructure, the national platform, the "
                                    "local engine, or the active candidate bot. The runtime "
                                    "baseline was advanced so later unrelated commits are "
                                    "checked incrementally. commit_bot still requires the "
                                    "canonical branch."
                                ),
                            },
                        )
                    continue
                reason = "head_drift"
            elif expected_branch and current_branch and current_branch != expected_branch:
                reason = "branch_drift"
            if not reason:
                continue

            payload = {
                "reason": reason,
                "expected_branch": expected_branch,
                "current_branch": current_branch,
                "expected_head": runtime_expected_head,
                "current_head": current_head,
                "branch_status": current.get("branch_status", ""),
                "directive": (
                    "Runtime evolution stopped because this shared worktree's "
                    "git branch/HEAD changed. Return to the expected branch and "
                    "restart so checkpoint recovery can revalidate the candidate."
                ),
            }
            msg = (
                "Runtime branch guard stopped evolution: "
                f"{reason} {expected_branch}@{runtime_expected_head} -> "
                f"{current_branch}@{current_head}"
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status("Stopped: git branch drift", is_working=False)
            else:
                log.error(msg)
            log_system_event("repo.runtime_branch_drift_shutdown", "error", msg, payload)
            _clear_orchestrator_session(reason="runtime_branch_drift")
            if hard_stop_event is not None:
                try:
                    hard_stop_event.set()
                except Exception:
                    pass
            if shutdown_mgr:
                shutdown_mgr.request_shutdown()
            if owner_task is not None and not owner_task.done():
                owner_task.cancel()
            return
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.debug("Runtime branch guard check error (non-fatal): %s", e)


def _stability_projection_maintenance_tick() -> None:
    """Request a proactive, still-fail-closed stability-cache refresh.

    The cache owns the remote verifier's single-flight lease.  This tick only
    supplies the current epoch authority and asks it to prefetch before its
    existing verified result expires; it never writes observation state or
    treats a pending/stale result as healthy.
    """

    from epoch_authority import epoch_stream_authority_digest, strict_epoch_projection
    from stability_observation import (
        STABILITY_VERIFICATION_PREFETCH_LEAD_SEC,
        stability_observation_cached_projection,
    )

    authority_digest = epoch_stream_authority_digest(strict_epoch_projection())
    if not isinstance(authority_digest, str) or len(authority_digest) != 64:
        raise RuntimeError("stability_maintenance_epoch_authority_unavailable")
    stability_observation_cached_projection(
        expected_epoch_authority_digest=authority_digest,
        prefetch_lead_sec=STABILITY_VERIFICATION_PREFETCH_LEAD_SEC,
    )


async def _stability_projection_maintenance_coroutine(
    shutdown_mgr,
    *,
    check_interval: float = STABILITY_OBSERVATION_MAINTENANCE_INTERVAL,
) -> None:
    """Keep the health cache verified without relying on browser polling."""

    interval = max(0.1, float(check_interval))
    while True:
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return
        try:
            await run_blocking_isolated(
                _stability_projection_maintenance_tick,
                thread_name_prefix="stability-observation-maintenance",
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Health remains fail-closed at the existing TTL boundary.  Do not
            # turn a maintenance diagnostic into an unbounded UI/event flood.
            log.debug("Stability maintenance refresh failed: %s", exc)
        if shutdown_mgr:
            try:
                await asyncio.wait_for(
                    shutdown_mgr.wait_for_shutdown(),
                    timeout=interval,
                )
            except asyncio.TimeoutError:
                continue
            return
        await asyncio.sleep(interval)


def _startup_recovery(ui=None):
    """Use the one strict checkpoint/handoff reader at process startup."""

    return _checkpoint_recovery_context(
        "startup",
        ui,
        log_level="warn",
        label="[Recovery]",
    )


def _startup_recovery_terminal_cost(recovery) -> float | None:
    """Map only canonical startup stop states to typed loop outcomes."""

    action = recovery.get("action") if isinstance(recovery, dict) else None
    if action == "blocked":
        return ORCH_RECOVERY_BLOCKED_COST
    if action == "operator_action_required":
        return ORCH_OPERATOR_ACTION_REQUIRED_COST
    return None


async def orchestrator_loop(
    ui,
    shutdown_mgr=None,
    no_daemon=False,
    daemon_workers=None,
    daemon_pairs=5,
    *,
    startup_recovery=_STARTUP_RECOVERY_UNSET,
):
    """Orchestrator entry point — three-phase generation loop.

    Args:
        ui: BaseUI instance (WebUI for Dashboard). Can be None for silent mode.
        shutdown_mgr: ShutdownManager for graceful signal handling.
        no_daemon: If True, skip daemon startup.
        daemon_workers: Number of parallel workers for the daemon subprocess.
        daemon_pairs: Complete 70-hand native matches per scheduled bot pairing.
    """
    try:
        from epoch_authority import require_policy_epoch_initialized

        require_policy_epoch_initialized("orchestrator_loop")
    except Exception as exc:
        state = getattr(exc, "state", {})
        epoch_state = state.get("state", "epoch_authority_unavailable")
        msg = (
            "Orchestrator not started: policy epoch initialization is "
            f"{epoch_state}"
        )
        if ui:
            ui.log_history(msg, "warn")
            ui.set_status(f"Stopped: {epoch_state}", is_working=False)
        log.error(msg)
        # Do not emit a structured event: its destination still belongs to the
        # retired epoch.  Web and CLI launchers expose the canonical state.
        return
    # Keep the orchestrator, daemon subprocess manager, web config and
    # stability identity on one resource contract.  The prior uncapped
    # CPU-derived default produced 28 workers on a 32-core host even though
    # daemon_management's OOM-safe authority caps the runtime at 12.
    daemon_workers = _resolve_daemon_workers(daemon_workers)
    from stability_observation import bind_runtime_configuration

    bind_runtime_configuration({
        "daemon_enabled": not no_daemon,
        "daemon_workers": int(daemon_workers),
        "daemon_pairs": int(daemon_pairs),
    })
    from tools import inject_ui
    inject_ui(ui)
    set_system_log_ui(ui)
    try:
        from llm_query import set_shutdown_manager
        set_shutdown_manager(shutdown_mgr)
    except Exception:
        pass

    # Parse once at the operator-facing process boundary.  The selected policy
    # is then passed internally; prompts, MCP calls, checkpoints, and candidate
    # artifacts have no field that can alter it.
    try:
        operator_cost_policy = configure_runtime_cost_policy(
            load_operator_generation_cost_policy()
        )
    except CostPolicyConfigurationError as exc:
        msg = f"Invalid operator generation cost policy: {exc}"
        if ui:
            ui.log_history(msg, "error")
            ui.set_status("Stopped: invalid operator cost policy", is_working=False)
        log.error(msg)
        log_system_event(
            "orchestrator.cost_policy_invalid",
            "error",
            msg,
            {"operator_action_required": True},
        )
        return 5

    os.makedirs(LOGS_DIR, exist_ok=True)
    _rotate_orchestrator_logs(LOGS_DIR)

    if ui:
        ui.log_history("🔥 Orchestrator starting...", "success")
        ui.set_header("🔥 LLM Orchestrator Evolution 🔥")

    # Canonical checkpoint/handoff recovery is the launch authority.  Prove it
    # before consuming the one-shot resume acknowledgement or clearing a durable
    # provider pause.  A CLI preflight may pass this exact object so the lower
    # loop cannot make a second, drifting startup decision.
    recovery = (
        _startup_recovery(ui)
        if startup_recovery is _STARTUP_RECOVERY_UNSET
        else startup_recovery
    )
    startup_terminal_cost = _startup_recovery_terminal_cost(recovery)
    recovery_stops_launch = startup_terminal_cost is not None

    pause_before_reconcile = None
    pause_after_reconcile = None
    if not recovery_stops_launch:
        try:
            pause_before_reconcile = load_llm_pause()
            # This is the parent-process launch boundary.  Consume and remove the
            # operator acknowledgement before daemon/SDK children can inherit it.
            pause_after_reconcile = consume_operator_resume_ack_from_env()
        except Exception as exc:
            msg = f"Invalid/unwritable LLM availability pause state: {exc}"
            if ui:
                ui.log_history(msg, "error")
                ui.set_status("Stopped: invalid LLM pause state", is_working=False)
            log.exception(msg)
            try:
                log_system_event(
                    "orchestrator.llm_availability_state_invalid",
                    "error",
                    msg,
                    {"operator_action_required": True},
                )
            except Exception:
                pass
            return 5
    if (
        pause_before_reconcile
        and pause_before_reconcile.get("active")
        and pause_after_reconcile
        and not pause_after_reconcile.get("active")
    ):
        resume_source = pause_after_reconcile.get("resume_source")
        msg = (
            "LLM availability pause cleared by "
            f"{resume_source}; deterministic checkpoint recovery will continue."
        )
        if ui:
            ui.log_history(msg, "info")
        log.info(msg)
        try:
            log_system_event(
                "orchestrator.llm_availability_resumed",
                "info",
                msg,
                {
                    "category": pause_after_reconcile.get("category"),
                    "evidence_digest": pause_after_reconcile.get("evidence_digest"),
                    "resume_source": resume_source,
                },
            )
        except Exception:
            pass

    log_system_event("orchestrator.started", "success", "Orchestrator started",
                     {
                         "daemon_enabled": not no_daemon,
                         "generation_cost_policy": operator_cost_policy.receipt(),
                     })
    log.info("Orchestrator loop started (daemon=%s)", not no_daemon)
    try:
        from evolution_infra import EVOLUTION_BRANCH
    except Exception:
        EVOLUTION_BRANCH = "main"
    _runtime_identity = _runtime_git_identity()
    _expected_runtime_head = (
        _runtime_identity.get("head", "")
        if _runtime_identity.get("branch") == EVOLUTION_BRANCH else ""
    )
    os.environ["POK_RUNTIME_EXPECTED_BRANCH"] = EVOLUTION_BRANCH
    _expected_runtime_head = _set_runtime_expected_head(_expected_runtime_head)
    _branch_guard_task = None
    _stability_maintenance_task = None
    _runtime_hard_stop_event = asyncio.Event()
    if not recovery_stops_launch and _runtime_branch_guard_enabled():
        _branch_guard_task = asyncio.create_task(
            _runtime_branch_guard_coroutine(
                ui,
                shutdown_mgr,
                expected_branch=EVOLUTION_BRANCH,
                expected_head=_expected_runtime_head,
                owner_task=asyncio.current_task(),
                hard_stop_event=_runtime_hard_stop_event,
            )
        )
        log_system_event(
            "repo.runtime_branch_guard_started",
            "info",
            "Runtime branch guard started",
            {
                "expected_branch": EVOLUTION_BRANCH,
                "expected_head": _expected_runtime_head,
                "current_branch": _runtime_identity.get("branch", ""),
                "current_head": _runtime_identity.get("head", ""),
                "check_interval": RUNTIME_BRANCH_GUARD_INTERVAL,
            },
        )
    if not recovery_stops_launch:
        _stability_maintenance_task = asyncio.create_task(
            _stability_projection_maintenance_coroutine(shutdown_mgr),
            name="stability-observation-maintenance",
        )

    # Start daemon only after recovery authority permits the workflow.
    _daemon_stop = None
    if not no_daemon and not recovery_stops_launch:
        from evolution_core import start_daemon, daemon_monitor_thread
        import threading
        try:
            start_daemon(workers=daemon_workers, pairs=daemon_pairs)
        except Exception as e:
            if ui:
                ui.log_history(f"Daemon start failed: {e}", "error")
            log.error("Daemon start failed: %s", e)
            no_daemon = True
        if not no_daemon:
            _daemon_stop = threading.Event()
            monitor = threading.Thread(
                target=daemon_monitor_thread,
                args=(ui, _daemon_stop, daemon_workers, daemon_pairs),
                daemon=True,
            )
            monitor.start()
            if ui:
                ui.log_history("Daemon started.", "info")

    log_file = LOGS_DIR / f"orchestrator_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    gen_count = 0
    consecutive_prep_fails = 0

    # Launch background watchdog coroutine to detect stuck pipelines
    _watchdog_task = asyncio.create_task(
        asyncio.sleep(0)
        if recovery_stops_launch
        else _watchdog_coroutine(ui, shutdown_mgr, check_interval=60)
    )
    terminal_outcome = 0.0
    consecutive_canonical_abandons = 0
    canonical_abandon_target = None

    def _reset_canonical_abandon_streak():
        nonlocal consecutive_canonical_abandons, canonical_abandon_target
        consecutive_canonical_abandons = 0
        canonical_abandon_target = None

    def _record_verified_canonical_abandon(
        *,
        checkpoint=None,
        terminal_proof=None,
        source="provider_cycle",
        gen_ctx=None,
    ):
        """Record one terminal abandon and stop repeated same-target churn."""

        nonlocal consecutive_canonical_abandons
        nonlocal canonical_abandon_target
        nonlocal terminal_outcome

        checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
        terminal_proof = (
            terminal_proof if isinstance(terminal_proof, dict) else {}
        )
        proof_identity = _canonical_abandon_proof_identity(terminal_proof)
        if proof_identity is None:
            terminal_outcome = ORCH_RECOVERY_BLOCKED_COST
            msg = (
                "Refusing successor preparation because a canonical-abandon "
                "sentinel arrived without an exact finalized transaction, "
                "ledger, and checkpoint proof."
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status(
                    "Stopped: canonical abandon proof unavailable",
                    is_working=False,
                )
            log.error(msg)
            log_system_event(
                "orchestrator.canonical_abandon_proof_blocked_stop",
                "error",
                msg,
                {
                    "source": source,
                    "gen_count": gen_count,
                    "checkpoint_present": bool(checkpoint),
                    "gen_ctx_next_v": getattr(gen_ctx, "next_v", None),
                    "gen_ctx_source_v": getattr(gen_ctx, "source_v", None),
                },
            )
            return True

        next_v = proof_identity["next_v"]
        source_v = proof_identity["source_v"]
        workflow_run_id = proof_identity["workflow_run_id"]
        checkpoint_matches_proof = (
            not checkpoint
            or (
                checkpoint.get("workflow_run_id") == workflow_run_id
                and checkpoint.get("next_v") == next_v
                and checkpoint.get("source_v") == source_v
                and type(checkpoint.get("checkpoint_revision")) is int
                and checkpoint["checkpoint_revision"]
                <= proof_identity["checkpoint_revision"]
            )
        )
        context_matches_proof = (
            gen_ctx is None
            or (
                getattr(gen_ctx, "next_v", None) == next_v
                and getattr(gen_ctx, "source_v", None) == source_v
            )
        )
        if (
            not checkpoint_matches_proof
            or not context_matches_proof
            or (not checkpoint and gen_ctx is None)
        ):
            terminal_outcome = ORCH_RECOVERY_BLOCKED_COST
            msg = (
                "Refusing successor preparation because canonical-abandon "
                "proof identity disagrees with the active checkpoint or "
                "generation context."
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status(
                    "Stopped: canonical abandon identity mismatch",
                    is_working=False,
                )
            log.error(msg)
            log_system_event(
                "orchestrator.canonical_abandon_proof_blocked_stop",
                "error",
                msg,
                {
                    "source": source,
                    "gen_count": gen_count,
                    "proof_identity": proof_identity,
                    "checkpoint": {
                        key: checkpoint.get(key)
                        for key in (
                            "workflow_run_id",
                            "next_v",
                            "source_v",
                            "checkpoint_revision",
                        )
                    },
                    "gen_ctx_next_v": getattr(gen_ctx, "next_v", None),
                    "gen_ctx_source_v": getattr(gen_ctx, "source_v", None),
                },
            )
            return True

        target = (next_v, source_v)
        if canonical_abandon_target == target:
            consecutive_canonical_abandons += 1
        else:
            canonical_abandon_target = target
            consecutive_canonical_abandons = 1
        payload = {
            "gen_count": gen_count,
            "source": source,
            "next_v": next_v,
            "source_v": source_v,
            "workflow_run_id": workflow_run_id,
            "checkpoint_revision": proof_identity["checkpoint_revision"],
            "checkpoint_stage": proof_identity["stage"],
            "consecutive_canonical_abandons": (
                consecutive_canonical_abandons
            ),
            "limit": MAX_CONSECUTIVE_CANONICAL_ABANDONS,
            "remaining": max(
                0,
                MAX_CONSECUTIVE_CANONICAL_ABANDONS
                - consecutive_canonical_abandons,
            ),
        }
        payload.update({
            "abandon_receipt_digest": terminal_proof[
                "abandon_receipt_digest"
            ],
            "abandon_transaction_id": terminal_proof["transaction_id"],
            "finalize_receipt_digest": terminal_proof[
                "finalize_receipt_digest"
            ],
        })
        deactivate_generation_cost_scope()
        if (
            consecutive_canonical_abandons
            >= MAX_CONSECUTIVE_CANONICAL_ABANDONS
        ):
            terminal_outcome = ORCH_CONSECUTIVE_ABANDON_LIMIT_COST
            payload["restart_required"] = True
            msg = (
                "Evolution stopped after "
                f"{consecutive_canonical_abandons} verified canonical "
                f"abandons for the same target v{next_v}; no successor "
                "workflow was prepared. Inspect the shared contract and "
                "explicitly restart after correction or review."
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status(
                    "Stopped: consecutive canonical abandon limit",
                    is_working=False,
                )
            log.error(msg)
            log_system_event(
                "orchestrator.consecutive_canonical_abandon_limit_stop",
                "error",
                msg,
                payload,
            )
            return True
        msg = (
            "Generation reached a verified canonical abandon boundary "
            f"({consecutive_canonical_abandons}/"
            f"{MAX_CONSECUTIVE_CANONICAL_ABANDONS} for target v{next_v}); "
            "the continuous outer scheduler may prepare one fresh successor "
            "workflow."
        )
        if ui:
            ui.log_history(msg, "warn")
        log.warning(msg)
        log_system_event(
            "orchestrator.generation_abandoned_handoff",
            "warn",
            msg,
            payload,
        )
        return False

    def _publication_accounting_allows_successor():
        """Reset the abandon streak only after durable accounting re-proves."""

        nonlocal terminal_outcome
        try:
            cost_status = generation_cost_status()
        except Exception as exc:
            cost_status = {
                "active": True,
                "accounting_ok": False,
                "accounting_errors": [
                    "generation_cost_status_unavailable:"
                    f"{type(exc).__name__}"
                ],
            }
        if (
            cost_status.get("active") is True
            and cost_status.get("accounting_ok") is not True
        ):
            terminal_outcome = ORCH_ACCOUNTING_BLOCKED_COST
            msg = (
                "Post-publication cleanup completed, but durable generation-"
                "cost accounting is invalid; refusing to prepare a successor "
                "workflow."
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status(
                    "Stopped: generation accounting invalid",
                    is_working=False,
                )
            log.error(
                "%s Errors: %s",
                msg,
                cost_status.get("accounting_errors"),
            )
            log_system_event(
                "orchestrator.accounting_blocked_stop",
                "error",
                msg,
                {
                    "accounting_errors": cost_status.get(
                        "accounting_errors"
                    ),
                    "generation_id": cost_status.get("generation_id"),
                },
            )
            return False
        _reset_canonical_abandon_streak()
        return True

    try:
        while True:
            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                break

            # Watchdog recovery: if background watchdog detected a stuck pipeline,
            # clear state and force a fresh cycle from the checkpoint stage.
            global _watchdog_triggered
            if _watchdog_triggered:
                _watchdog_triggered = False
                if ui:
                    ui.log_history("[Watchdog] Restarting cycle from checkpoint stage.", "warn")
                recovery = _checkpoint_recovery_context("watchdog_recovery", ui)
                # Restart watchdog for the new cycle
                if _watchdog_task.done():
                    _watchdog_task = asyncio.create_task(
                        _watchdog_coroutine(ui, shutdown_mgr, check_interval=60)
                    )

            # 429 quota exhaustion check — block until reset, then dispatch a
            # fresh provider stream from the validated checkpoint.
            from rate_limiter import rate_limiter
            if rate_limiter.is_blocked():
                wait = rate_limiter.wait_seconds()
                if ui:
                    ui.log_history(
                        f"⏳ API 配额耗尽，暂停进化。将在 {rate_limiter.reset_time_str()} 自动恢复 ({wait:.0f}s)",
                        "warn",
                    )
                    ui.set_status(f"⏳ 配额等待中 → {rate_limiter.reset_time_str()}", is_working=False)
                await rate_limiter.wait_until_reset(shutdown_mgr=shutdown_mgr)
                continue

            if recovery is None:
                recovery = _checkpoint_recovery_context("active_checkpoint", ui)

            gen_count += 1
            log_system_event("orchestrator.cycle_start", "info", f"Cycle {gen_count} starting",
                             {"gen_count": gen_count})

            if recovery and recovery.get("action") == "operator_action_required":
                terminal_outcome = ORCH_OPERATOR_ACTION_REQUIRED_COST
                checkpoint = recovery.get("checkpoint") or {}
                msg = (
                    "Startup recovery is parked at the operator-only official "
                    f"bootstrap boundary for v{checkpoint.get('next_v')}."
                )
                if ui:
                    ui.log_history(f"[Orchestrator] {msg}", "warn")
                    ui.set_status(
                        "Stopped: operator action required",
                        is_working=False,
                    )
                log.warning(msg)
                log_system_event(
                    "orchestrator.operator_action_required_stop",
                    "warn",
                    msg,
                    {
                        "next_v": checkpoint.get("next_v"),
                        "source_v": checkpoint.get("source_v"),
                        "stage": checkpoint.get("stage"),
                    },
                )
                break

            if recovery and recovery.get("action") == "blocked":
                terminal_outcome = ORCH_RECOVERY_BLOCKED_COST
                diag = recovery.get("diagnostics") or {}
                issues = diag.get("issues") or []
                msg = (
                    "Startup recovery is blocked by an unrecoverable pipeline "
                    f"checkpoint: {', '.join(map(str, issues)) or recovery.get('reason')}"
                )
                if ui:
                    ui.log_history(f"[Orchestrator] {msg}", "error")
                    ui.set_status(
                        "Recovery blocked; governed diagnostics/operator action required",
                        is_working=False,
                    )
                log.error(msg)
                log_system_event(
                    "orchestrator.recovery_blocked_stop",
                    "error",
                    msg,
                    {
                        "reason": recovery.get("reason"),
                        "issues": issues,
                        "diagnostics": diag,
                    },
                )
                break

            # If recovering, skip Phase 1 (context already known from checkpoint)
            if recovery and recovery.get("action") == "resume":
                route_log_kwargs = _recovery_route_log_kwargs(recovery)
                advanced = await _advance_deterministic_recovery(
                    recovery,
                    ui,
                    cost_policy=operator_cost_policy,
                    shutdown_mgr=shutdown_mgr,
                    gen_count=gen_count,
                    **route_log_kwargs,
                )
                if advanced["routed"]:
                    if advanced["terminal_action"] == "generation_abandoned":
                        stopped = _record_verified_canonical_abandon(
                            checkpoint=(recovery or {}).get("checkpoint"),
                            terminal_proof=(
                                advanced.get("terminal_proof") or {}
                            ),
                            source="deterministic_recovery",
                        )
                        recovery = advanced["recovery"]
                        if stopped:
                            break
                        await asyncio.sleep(0)
                        continue
                    if (
                        advanced["terminal_action"]
                        == "publication_handoff_completed"
                    ):
                        if not _publication_accounting_allows_successor():
                            break
                    recovery = advanced["recovery"]
                    await asyncio.sleep(1)
                    continue
                ckpt = recovery["checkpoint"]
                gen_ctx = _generation_context_from_checkpoint(
                    ckpt,
                    gen_count=gen_count,
                )
                recovery = None  # consume recovery, only used once
            else:
                # Phase 1: Prepare (disposable on interrupt)
                # Do not create a fresh candidate while a provider pause is
                # active. Existing deterministic recovery routes are attempted
                # above first, which lets the system strict bootstrap advance
                # without any LLM dependency.
                if not await _honor_active_llm_pause(ui, shutdown_mgr):
                    break
                # Use degraded min_games after repeated eval timeouts
                degraded_min = None
                if consecutive_prep_fails >= 3:
                    degraded_min = 30
                    if ui:
                        ui.log_history("评估等待连续超时，降低评估要求 (30 局) 继续进化...", "warn")

                gen_ctx = await _prepare_or_fail(shutdown_mgr, ui, min_games=degraded_min)
                if gen_ctx is None:
                    if shutdown_mgr and shutdown_mgr.is_shutting_down:
                        break
                    consecutive_prep_fails += 1
                    from evolution_infra import is_daemon_alive
                    if not is_daemon_alive() and ui:
                        daemon_dead_level = "error" if consecutive_prep_fails >= 3 else "warn"
                        ui.log_history(
                            f"Daemon 未运行，等待恢复中... (连续失败 {consecutive_prep_fails} 次)",
                            daemon_dead_level,
                        )
                    backoff = min(10 * (2 ** min(consecutive_prep_fails - 1, 4)), 300)
                    if shutdown_mgr:
                        try:
                            await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=backoff)
                            break
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(backoff)
                    continue
                consecutive_prep_fails = 0

                selected_recovery = _checkpoint_recovery_context(
                    "selected_after_prepare",
                    ui,
                    log_level="info",
                    label="[Pipeline]",
                )
                if selected_recovery and selected_recovery.get("action") in {
                    "blocked",
                    "operator_action_required",
                }:
                    recovery = selected_recovery
                    continue
                if selected_recovery and selected_recovery.get("action") == "resume":
                    advanced = await _advance_deterministic_recovery(
                        selected_recovery,
                        ui,
                        log_level="info",
                        label="[Pipeline]",
                        cost_policy=operator_cost_policy,
                        shutdown_mgr=shutdown_mgr,
                        gen_ctx=gen_ctx,
                        gen_count=gen_count,
                    )
                    if advanced["routed"]:
                        if advanced["terminal_action"] == "generation_abandoned":
                            stopped = _record_verified_canonical_abandon(
                                checkpoint=(
                                    selected_recovery.get("checkpoint") or {}
                                ),
                                terminal_proof=(
                                    advanced.get("terminal_proof") or {}
                                ),
                                source="selected_deterministic_recovery",
                                gen_ctx=gen_ctx,
                            )
                            recovery = advanced["recovery"]
                            if stopped:
                                break
                            await asyncio.sleep(0)
                            continue
                        if (
                            advanced["terminal_action"]
                            == "publication_handoff_completed"
                            and not _publication_accounting_allows_successor()
                        ):
                            break
                        recovery = advanced["recovery"]
                        await asyncio.sleep(1)
                        continue

            # Phase 2: Run one generation (preserves state on interrupt). A
            # deterministic route has already had priority; any remaining work
            # needs the Orchestrator LLM and must honor the durable pause.
            if not await _honor_active_llm_pause(ui, shutdown_mgr):
                if not (shutdown_mgr and shutdown_mgr.is_shutting_down):
                    terminal_outcome = ORCH_LLM_AVAILABILITY_BLOCKED_COST
                break
            cost = await _run_one_cycle(
                ui=ui,
                log_file=log_file,
                one_gen=False,
                dry_run=False,
                max_turns=None,
                gen_ctx=gen_ctx,
                shutdown_mgr=shutdown_mgr,
                _cost_policy=operator_cost_policy,
            )

            if cost == ORCH_OPERATOR_COST_LIMIT_COST:
                terminal_outcome = ORCH_OPERATOR_COST_LIMIT_COST
                msg = (
                    "Orchestrator stopped at the explicit operator generation cost limit. "
                    "The checkpoint is preserved; change/disable the parent-process limit "
                    "and explicitly restart to continue."
                )
                if ui:
                    ui.log_history(msg, "error")
                    ui.set_status("Stopped: operator generation cost limit", is_working=False)
                log.error(msg)
                break

            if cost == ORCH_LLM_AVAILABILITY_BLOCKED_COST:
                # A persisted manual pause ends the loop immediately; a
                # transient pause waits for its bounded cooldown and then
                # resumes from the exact active checkpoint. If persistence
                # itself failed, fail closed instead of retrying blindly.
                try:
                    pause_state = load_llm_pause()
                except Exception as exc:
                    pause_state = None
                    log.error("Cannot read LLM availability pause after block: %s", exc)
                if not pause_state or not pause_state.get("active"):
                    terminal_outcome = ORCH_LLM_AVAILABILITY_BLOCKED_COST
                    msg = (
                        "LLM availability was classified but its durable pause "
                        "record is unavailable; stopping fail-closed."
                    )
                    if ui:
                        ui.log_history(msg, "error")
                        ui.set_status("Stopped: LLM pause persistence failed", is_working=False)
                    log.error(msg)
                    break
                if not await _honor_active_llm_pause(ui, shutdown_mgr):
                    if not (shutdown_mgr and shutdown_mgr.is_shutting_down):
                        terminal_outcome = ORCH_LLM_AVAILABILITY_BLOCKED_COST
                    break
                recovery = _checkpoint_recovery_context(
                    "llm_availability_resumed", ui
                )
                continue

            if cost == ORCH_GENERATION_ABANDONED_COST:
                stopped = _record_verified_canonical_abandon(
                    source="provider_cycle",
                    gen_ctx=gen_ctx,
                    terminal_proof=(
                        _remembered_canonical_abandon_proof(gen_ctx) or {}
                    ),
                )
                recovery = None
                if stopped:
                    break
                await asyncio.sleep(0)
                continue

            if cost == ORCH_OPERATOR_ACTION_REQUIRED_COST:
                terminal_outcome = ORCH_OPERATOR_ACTION_REQUIRED_COST
                msg = (
                    "Generation is parked at an operator-only boundary. "
                    "Automatic evolution stopped without preparing a successor."
                )
                if ui:
                    ui.log_history(msg, "warn")
                    ui.set_status(
                        "Stopped: operator action required",
                        is_working=False,
                    )
                log.warning(msg)
                log_system_event(
                    "orchestrator.operator_action_required_stop",
                    "warn",
                    msg,
                    {"gen_count": gen_count},
                )
                break

            if cost == ORCH_RECOVERY_BLOCKED_COST:
                terminal_outcome = ORCH_RECOVERY_BLOCKED_COST
                msg = (
                    "Orchestrator stopped fail-closed because checkpoint or "
                    "terminal-generation authority could not be re-proven. "
                    "Do not prepare another generation until governed recovery "
                    "diagnostics are resolved."
                )
                if ui:
                    ui.log_history(msg, "error")
                    ui.set_status(
                        "Stopped: recovery authority blocked",
                        is_working=False,
                    )
                log.error(msg)
                log_system_event(
                    "orchestrator.recovery_authority_blocked_stop",
                    "error",
                    msg,
                    {"cost_signal": cost},
                )
                break

            # Timeout-extension sentinel: a cycle timed out but commit was imminent
            # (stage=verified) so ONE extension was granted mid-cycle. The cycle is NOT
            # complete — the bot has not committed yet. Do NOT run post_generation_cleanup,
            # do NOT log 'gen complete', do NOT back off. Just resume from the checkpoint
            # next iteration. Must come BEFORE the cost >= 0 success block so the sentinel
            # is never treated as success. Value -99999.0 (distinct from auth clamp).
            if cost == ORCH_ACTIONABLE_HANDOFF_COST:
                recovery = _checkpoint_recovery_context(
                    "actionable_stage_handoff",
                    ui,
                    log_level="info",
                    label="[Pipeline]",
                )
                if recovery:
                    if recovery.get("action") == "resume":
                        advanced = await _advance_deterministic_recovery(
                            recovery,
                            ui,
                            log_level="info",
                            label="[Pipeline]",
                            cost_policy=operator_cost_policy,
                            shutdown_mgr=shutdown_mgr,
                            gen_ctx=gen_ctx,
                            gen_count=gen_count,
                        )
                        if advanced["routed"]:
                            if (
                                advanced["terminal_action"]
                                == "generation_abandoned"
                            ):
                                stopped = _record_verified_canonical_abandon(
                                    checkpoint=(recovery or {}).get(
                                        "checkpoint"
                                    ),
                                    terminal_proof=(
                                        advanced.get("terminal_proof") or {}
                                    ),
                                    source="actionable_deterministic_recovery",
                                    gen_ctx=gen_ctx,
                                )
                                recovery = advanced["recovery"]
                                if stopped:
                                    break
                                await asyncio.sleep(0)
                                continue
                            if (
                                advanced["terminal_action"]
                                == "publication_handoff_completed"
                                and not _publication_accounting_allows_successor()
                            ):
                                break
                            recovery = advanced["recovery"]
                            await asyncio.sleep(1)
                        else:
                            await asyncio.sleep(0)
                    continue
                recovery = {
                    "action": "blocked",
                    "reason": "actionable_handoff_authority_missing",
                    "checkpoint": None,
                    "diagnostics": {
                        "active": True,
                        "recoverable": False,
                        "issues": ["actionable_handoff_authority_missing"],
                    },
                }
                continue

            if cost == -99999.0:
                if ui:
                    ui.log_history(
                        "Orchestrator: cycle timed out but commit was imminent — granted extension, "
                        "resuming from checkpoint next cycle (no commit yet).",
                        "warn",
                    )
                continue

            if cost == SHUTDOWN_CANCEL_COST:
                if ui:
                    ui.log_history(
                        "Orchestrator: shutdown cancellation observed; exiting loop without backoff.",
                        "warn",
                    )
                break

            # Phase 3: Cleanup (idempotent) — after any successful generation
            if cost >= 0:
                active_recovery = _checkpoint_recovery_context("cycle_completed_with_active_checkpoint", ui)
                if active_recovery:
                    recovery = active_recovery
                    if ui:
                        ui.log_history(
                            "Orchestrator cycle ended while checkpoint is still active; "
                            "continuing from checkpoint instead of marking generation complete.",
                            "warn",
                        )
                    try:
                        ckpt = active_recovery.get("checkpoint") or {}
                        log_system_event(
                            "orchestrator.cycle_yielded_active_checkpoint",
                            "warn",
                            "Cycle ended with active checkpoint; skipping post-generation cleanup",
                            {
                                "gen_count": gen_count,
                                "stage": ckpt.get("stage"),
                                "next_v": ckpt.get("next_v"),
                                "source_v": ckpt.get("source_v"),
                                "cost": round(cost, 4),
                            },
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(5)
                    continue
                # Reset the generic-failure backoff counter — the cycle succeeded.
                if getattr(orchestrator_loop, "_gen_fail_count", 0):
                    orchestrator_loop._gen_fail_count = 0
                cleanup_ok = await _run_post_generation_cleanup_with_timeout(
                    shutdown_mgr, ui, gen_ctx, gen_count=gen_count
                )
                if cleanup_ok is not True:
                    terminal_outcome = ORCH_RECOVERY_BLOCKED_COST
                    msg = (
                        "Post-generation verification did not complete; "
                        "stopping before any successor generation is prepared."
                    )
                    if ui:
                        ui.log_history(msg, "error")
                        ui.set_status(
                            "Stopped: post-generation verification failed",
                            is_working=False,
                        )
                    log.error(msg)
                    log_system_event(
                        "orchestrator.post_cleanup_verification_blocked_stop",
                        "error",
                        msg,
                        {"gen_count": gen_count, "cost": round(cost, 4)},
                    )
                    break
                if ui:
                    ui.log_history(f"Orchestrator gen {gen_count} complete. Cost: ${cost:.4f}", "info")
                log_system_event("orchestrator.cycle_done", "info", f"Cycle {gen_count} done (cost=${cost:.4f})",
                                 {"gen_count": gen_count, "cost": round(cost, 4)})
                # Reset per-generation cost tracker for next cycle
                if ui:
                    ui.reset_gen_cost()
                _reset_canonical_abandon_streak()
                deactivate_generation_cost_scope()

            # Auth error fast-fail (also catches 429 via negative cost from _stream_response)
            if cost < 0:
                # 429 quota — rate_limiter already set, loop top will handle blocking
                from rate_limiter import rate_limiter
                if rate_limiter.is_blocked():
                    continue

                # P2: LLM infra error (SDK signature/timeout/connection) — short backoff.
                # cost == -0.5 sentinel from cycle_failed+infra_error path. Session already
                # cleared inside _run_one_cycle's except handler, so no redundant clear here.
                # Was previously misclassified as "API auth error (401/403)" with 300s backoff
                # (the v97 1.5h stuck loop: signature storm → -1.0 → 300s → restart → repeat).
                if cost == -0.5:
                    _infra_backoff = 15
                    if ui:
                        ui.log_history(
                            f"Orchestrator: LLM infrastructure error (SDK signature/timeout/connection). "
                            f"Backing off {_infra_backoff}s (short, NOT auth).", "warn")
                    try:
                        log_system_event("pipeline.infra_error_short_backoff", "warn",
                            f"Infra error short backoff {_infra_backoff}s",
                            {"cost_signal": cost})
                    except Exception:
                        pass
                    if shutdown_mgr:
                        try:
                            await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=_infra_backoff)
                            break
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(_infra_backoff)
                    # Session already cleared in _run_one_cycle except handler (infra path).
                    # Preserve the generation identity by resuming from the active checkpoint
                    # on the next loop; otherwise Phase 1 may select a new source/crossover
                    # while pipeline_state.json still points at the interrupted generation.
                    recovery = _checkpoint_recovery_context("infra_error", ui)
                    continue

                # cost <= -1.0 lands here. Two distinct causes share this signal:
                #   (a) a genuine auth failure (401/403, set auth_error=True above) —
                #       credentials won't self-heal, so a long backoff is correct.
                #   (b) a generic cycle failure (crash/ProcessError/exit-143) with NO
                #       auth_error flag — usually another face of the transient SDK
                #       signature storm. Treating these as auth and waiting 300s each
                #       time turned a brief SDK hiccup into a multi-hour stuck loop.
                # Split them: real auth keeps 300s; generic failures get a short,
                # escalating backoff (30s -> 60s -> 120s -> cap 300s).
                # auth_error 只在 _run_one_cycle 作用域声明，orchestrator_loop 无法直接读
                # (root-cause-audit 2026-06-21: 引用未定义变量致 NameError crash)。从 cost
                # 推断：auth 失败返回 -max(abs(cost),1.0) (< -1.0)，generic crash 返回 -1.0。
                auth_error = cost < -1.0
                if auth_error:
                    if ui:
                        ui.log_history("Orchestrator: API auth error (401/403). Backing off 300s.", "error")
                    _wait = 300
                else:
                    _gen_fail_count = getattr(orchestrator_loop, "_gen_fail_count", 0) + 1
                    orchestrator_loop._gen_fail_count = _gen_fail_count
                    _wait = min(30 * (2 ** min(_gen_fail_count - 1, 3)), 300)
                    if ui:
                        ui.log_history(
                            f"Orchestrator: cycle failed (generic, not auth). "
                            f"Backing off {_wait}s (consecutive #{_gen_fail_count}).", "warn")
                if shutdown_mgr:
                    try:
                        await asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=_wait)
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(_wait)
                _clear_orchestrator_session()
                continue

            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                break

            await asyncio.sleep(5)

    except OperatorGenerationCostLimitExceeded as exc:
        # Deterministic checkpoint routes can execute LLM roles without opening
        # an Orchestrator SDK stream.  Park those paths at the same operator
        # boundary instead of reporting a generic orchestrator crash.
        _clear_orchestrator_session(reason="operator_generation_cost_limit")
        if ui:
            ui.set_status("Stopped: operator generation cost limit", is_working=False)
            ui.log_history(str(exc), "error")
            _project_generation_cost_runtime(ui)
        log.error("Operator generation cost limit stopped evolution: %s", exc)
        terminal_outcome = ORCH_OPERATOR_COST_LIMIT_COST
    except LLMAvailabilityBlocked as exc:
        # Defensive boundary for an LLM role outside the normal stream/direct
        # route wrappers. Never relabel a provider stop as an orchestrator crash.
        try:
            persist_llm_pause(exc)
        except Exception as pause_exc:
            log.exception("Failed to persist outer-loop LLM pause: %s", pause_exc)
        _clear_orchestrator_session(reason="outer_llm_availability_blocked")
        if ui:
            ui.set_status(f"Stopped: LLM unavailable ({exc.issue.category})", is_working=False)
            ui.log_history(str(exc), "error")
        log.error("LLM availability stopped evolution: %s", exc)
        terminal_outcome = ORCH_LLM_AVAILABILITY_BLOCKED_COST
    except LLMAvailabilityPauseError as exc:
        _clear_orchestrator_session(reason="llm_availability_state_invalid")
        if ui:
            ui.set_status("Stopped: LLM availability state invalid", is_working=False)
            ui.log_history(str(exc), "error")
        log.error("LLM availability control stopped evolution: %s", exc)
        terminal_outcome = ORCH_LLM_AVAILABILITY_BLOCKED_COST
    except asyncio.CancelledError:
        if ui:
            ui.set_status("Stopped", is_working=False)
            ui.log_history("Orchestrator stopped.", "warn")
        log_system_event("orchestrator.stopped", "warn", "Orchestrator stopped")
        try:
            from server.state import app_state
            app_state.set_running(False)
        except Exception as e:
            log.debug("Loop cleanup error: %s", e)
    except Exception as e:
        if ui:
            ui.log_history(f"Orchestrator crashed: {e}", "error")
        log_system_event("orchestrator.crashed", "error", f"Orchestrator crashed: {e}",
                         {"error": str(e)[:200]})
        _clear_orchestrator_session()
        # Preserve checkpoint for crash recovery regardless of error type.
        # The checkpoint stage-tracking allows startup recovery to assess state.
        try:
            from server.state import app_state
            app_state.set_running(False)
        except Exception as e:
            log.debug("Loop error cleanup: %s", e)
        terminal_outcome = -1.0
    finally:
        try:
            from server.state import app_state
            app_state.set_running(False)
        except Exception as e:
            log.debug("Loop final cleanup error: %s", e)
        if _branch_guard_task is not None and not _branch_guard_task.done():
            _branch_guard_task.cancel()
            try:
                await _branch_guard_task
            except asyncio.CancelledError:
                pass
        if (
            _stability_maintenance_task is not None
            and not _stability_maintenance_task.done()
        ):
            _stability_maintenance_task.cancel()
            try:
                await _stability_maintenance_task
            except asyncio.CancelledError:
                pass
        if not _watchdog_task.done():
            _watchdog_task.cancel()
            try:
                await _watchdog_task
            except asyncio.CancelledError:
                pass
        if _daemon_stop is not None:
            _daemon_stop.set()
        if _runtime_hard_stop_event.is_set():
            try:
                from daemon_management import stop_daemon
                await run_blocking_isolated(
                    stop_daemon,
                    thread_name_prefix="daemon-shutdown",
                )
                log_system_event(
                    "repo.runtime_branch_drift_cleanup",
                    "info",
                    "Stopped daemon after runtime branch drift",
                )
            except Exception as e:
                log_system_event(
                    "repo.runtime_branch_drift_cleanup_failed",
                    "warn",
                    f"Failed to stop daemon after runtime branch drift: {e}",
                    {"error": str(e)[:300]},
                )
        # For normal orchestrator exits, don't stop daemon — it runs independently
        # and survives orchestrator restarts. Full process exit/app shutdown and
        # runtime branch-drift hard stop are the exceptions.
    return terminal_outcome


async def _prepare_or_fail(shutdown_mgr, ui, min_games=None):
    """Run prepare_generation with error handling. Returns ctx or None."""
    from generation_scheduler import prepare_generation
    try:
        return await prepare_generation(shutdown_mgr, ui, min_games=min_games)
    except asyncio.CancelledError:
        raise
    except OperatorGenerationCostLimitExceeded:
        # Operator policy is a terminal outer-loop control signal, not a
        # disposable prepare failure eligible for exponential retry.
        raise
    except LLMAvailabilityBlocked:
        # Provider availability is a durable outer-loop pause.  Returning None
        # would turn it into a disposable prepare retry and lose typed control.
        raise
    except Exception as e:
        if ui:
            ui.log_history(f"prepare_generation failed: {e}", "error")
        else:
            log.error("prepare_generation failed: %s", e)
        return None


def _generation_context_from_checkpoint(checkpoint, *, gen_count=1):
    from generation_scheduler import GenerationContext

    parent2_v = checkpoint.get("parent2_v")
    source_v = int(checkpoint["source_v"])
    next_v = int(checkpoint["next_v"])
    return GenerationContext(
        current_v=source_v,
        next_v=next_v,
        strategy="crossover" if parent2_v else "master",
        source_v=source_v,
        crossover_parents=(source_v, int(parent2_v)) if parent2_v else (),
        gen_count=int(gen_count),
    )


def _one_generation_binding(checkpoint):
    if not isinstance(checkpoint, dict):
        return None
    workflow_run_id = str(checkpoint.get("workflow_run_id") or "").strip()
    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    if (
        not workflow_run_id
        or type(next_v) is not int
        or type(source_v) is not int
    ):
        return None
    return workflow_run_id, next_v, source_v


async def _run_one_generation_cli_impl(
    *,
    log_file,
    max_turns,
    shutdown_mgr,
    cost_policy,
    startup_recovery=_STARTUP_RECOVERY_UNSET,
):
    """Drive one exact workflow across fresh provider/deterministic handoffs."""

    from generation_scheduler import prepare_generation

    recovery = (
        _checkpoint_recovery_context(
            "one_gen_start",
            None,
            log_level="info",
            label="[OneGen]",
        )
        if startup_recovery is _STARTUP_RECOVERY_UNSET
        else startup_recovery
    )
    bound_identity = None
    gen_ctx = None
    prepared = False
    accumulated_cost = 0.0

    for transition in range(512):
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return SHUTDOWN_CANCEL_COST
        if recovery and recovery.get("action") == "blocked":
            log.error(
                "One-gen recovery is blocked: %s",
                recovery.get("diagnostics"),
            )
            return ORCH_RECOVERY_BLOCKED_COST

        if recovery is None:
            if bound_identity is not None:
                log.error(
                    "One-gen workflow %s lost checkpoint/handoff authority; "
                    "refusing to prepare a successor.",
                    bound_identity[0],
                )
                return ORCH_RECOVERY_BLOCKED_COST
            if prepared:
                return ORCH_RECOVERY_BLOCKED_COST
            if not await _honor_active_llm_pause(None, shutdown_mgr):
                return ORCH_LLM_AVAILABILITY_BLOCKED_COST
            gen_ctx = await prepare_generation(shutdown_mgr, None)
            prepared = True
            if gen_ctx is None:
                return (
                    SHUTDOWN_CANCEL_COST
                    if shutdown_mgr and shutdown_mgr.is_shutting_down
                    else -1.0
                )
            recovery = _checkpoint_recovery_context(
                "one_gen_prepared",
                None,
                log_level="info",
                label="[OneGen]",
            )
            if not recovery:
                log.error(
                    "One-gen prepare returned without a durable checkpoint."
                )
                return ORCH_RECOVERY_BLOCKED_COST
            continue

        checkpoint = recovery.get("checkpoint") or {}
        identity = _one_generation_binding(checkpoint)
        if identity is None:
            log.error("One-gen recovery checkpoint identity is invalid.")
            return ORCH_RECOVERY_BLOCKED_COST
        if bound_identity is None:
            bound_identity = identity
            gen_ctx = _generation_context_from_checkpoint(checkpoint)
        elif identity != bound_identity:
            log.error(
                "One-gen workflow identity drifted: %s -> %s",
                bound_identity,
                identity,
            )
            return ORCH_RECOVERY_BLOCKED_COST

        if checkpoint.get("stage") == "official_bootstrap_required":
            log.warning(
                "One-gen workflow %s parked at operator-only first-strict "
                "bootstrap.",
                bound_identity[0],
            )
            return ORCH_OPERATOR_ACTION_REQUIRED_COST

        advanced = await _advance_deterministic_recovery(
            recovery,
            None,
            log_level="info",
            label="[OneGen]",
            cost_policy=cost_policy,
            shutdown_mgr=shutdown_mgr,
            gen_ctx=gen_ctx,
            gen_count=1,
        )
        if advanced["routed"]:
            recovery = advanced["recovery"]
            if advanced["terminal_action"] == "generation_abandoned":
                if not _remember_verified_canonical_abandon(
                    gen_ctx,
                    advanced.get("terminal_proof"),
                ):
                    log.error(
                        "One-gen deterministic abandon lacked a proof bound "
                        "to its active generation context."
                    )
                    return ORCH_RECOVERY_BLOCKED_COST
                deactivate_generation_cost_scope()
                return ORCH_GENERATION_ABANDONED_COST
            if advanced["terminal_action"] == "publication_handoff_completed":
                cost_status = generation_cost_status()
                if (
                    cost_status.get("active") is True
                    and cost_status.get("accounting_ok") is not True
                ):
                    log.error(
                        "One-gen completed publication but durable cost "
                        "accounting is invalid: %s",
                        cost_status.get("accounting_errors"),
                    )
                    return ORCH_ACCOUNTING_BLOCKED_COST
                return (
                    float(cost_status.get("spent_usd") or 0.0)
                    if cost_status.get("active") is True
                    else accumulated_cost
                )
            continue

        if not await _honor_active_llm_pause(None, shutdown_mgr):
            return ORCH_LLM_AVAILABILITY_BLOCKED_COST
        cost = await _run_one_cycle(
            ui=None,
            log_file=log_file,
            one_gen=True,
            dry_run=False,
            max_turns=max_turns,
            gen_ctx=gen_ctx,
            shutdown_mgr=shutdown_mgr,
            _cost_policy=cost_policy,
        )
        if cost == ORCH_GENERATION_ABANDONED_COST:
            if _remembered_canonical_abandon_proof(gen_ctx) is None:
                log.error(
                    "One-gen provider abandon lacked a proof bound to its "
                    "active generation context."
                )
                return ORCH_RECOVERY_BLOCKED_COST
            deactivate_generation_cost_scope()
            return cost
        if cost in {
            ORCH_RECOVERY_BLOCKED_COST,
            ORCH_OPERATOR_ACTION_REQUIRED_COST,
            ORCH_ACCOUNTING_BLOCKED_COST,
            ORCH_OPERATOR_COST_LIMIT_COST,
            ORCH_LLM_AVAILABILITY_BLOCKED_COST,
            SHUTDOWN_CANCEL_COST,
        }:
            return cost
        if cost == ORCH_ACTIONABLE_HANDOFF_COST:
            recovery = _checkpoint_recovery_context(
                "one_gen_actionable_handoff",
                None,
                log_level="info",
                label="[OneGen]",
            )
            if recovery is None:
                return ORCH_RECOVERY_BLOCKED_COST
            continue
        if cost < 0:
            return cost
        accumulated_cost += float(cost)
        recovery = _checkpoint_recovery_context(
            "one_gen_provider_cycle_completed",
            None,
            log_level="info",
            label="[OneGen]",
        )
        if recovery is None:
            log.error(
                "One-gen provider cycle ended without an active workflow, "
                "publication handoff, or canonical abandon proof."
            )
            return ORCH_RECOVERY_BLOCKED_COST

    log.error("One-gen exceeded the bounded 512-transition workflow driver.")
    return ORCH_RECOVERY_BLOCKED_COST


async def _run_one_generation_cli(
    *,
    log_file,
    max_turns,
    shutdown_mgr,
    cost_policy,
    startup_recovery=_STARTUP_RECOVERY_UNSET,
):
    """Map every one-generation control failure to a documented CLI class."""

    try:
        return await _run_one_generation_cli_impl(
            log_file=log_file,
            max_turns=max_turns,
            shutdown_mgr=shutdown_mgr,
            cost_policy=cost_policy,
            startup_recovery=startup_recovery,
        )
    except asyncio.CancelledError:
        raise
    except OperatorGenerationCostLimitExceeded as exc:
        _clear_orchestrator_session(reason="one_gen_operator_cost_limit")
        log.error("One-gen stopped at operator cost limit: %s", exc)
        return ORCH_OPERATOR_COST_LIMIT_COST
    except LLMAvailabilityBlocked as exc:
        try:
            persist_llm_pause(exc)
        except Exception as pause_exc:
            log.exception(
                "One-gen failed to persist provider pause state: %s",
                pause_exc,
            )
        _clear_orchestrator_session(reason="one_gen_llm_availability_blocked")
        log.error("One-gen stopped on provider availability: %s", exc)
        return ORCH_LLM_AVAILABILITY_BLOCKED_COST
    except LLMAvailabilityPauseError as exc:
        _clear_orchestrator_session(reason="one_gen_llm_pause_state_invalid")
        log.error("One-gen LLM pause control failed closed: %s", exc)
        return ORCH_LLM_AVAILABILITY_BLOCKED_COST
    except Exception as exc:
        log.exception("One-gen control failure: %s", exc)
        try:
            log_system_event(
                "orchestrator.one_gen_control_failed",
                "error",
                f"One-gen control failure: {type(exc).__name__}",
                {"error": str(exc)[:500]},
            )
        except Exception:
            pass
        return -1.0


def _one_generation_exit_code(cost) -> int:
    if cost >= 0:
        return 0
    if cost == ORCH_GENERATION_ABANDONED_COST:
        return 2
    if cost == ORCH_OPERATOR_ACTION_REQUIRED_COST:
        return 3
    if cost == ORCH_RECOVERY_BLOCKED_COST:
        return 4
    if cost == ORCH_ACCOUNTING_BLOCKED_COST:
        return 6
    if cost == ORCH_CONSECUTIVE_ABANDON_LIMIT_COST:
        return 7
    return 5


def _continuous_exit_code(outcome) -> int:
    """Map the loop's typed terminal outcome to a process exit status."""

    if outcome is None or outcome == 0:
        return 0
    if outcome in {
        ORCH_GENERATION_ABANDONED_COST,
        ORCH_OPERATOR_ACTION_REQUIRED_COST,
        ORCH_RECOVERY_BLOCKED_COST,
        ORCH_ACCOUNTING_BLOCKED_COST,
        ORCH_CONSECUTIVE_ABANDON_LIMIT_COST,
        ORCH_OPERATOR_COST_LIMIT_COST,
        ORCH_LLM_AVAILABILITY_BLOCKED_COST,
    }:
        return _one_generation_exit_code(outcome)
    if type(outcome) is int and outcome > 0:
        return outcome
    return 5


async def run_orchestrator_cli(args, shutdown_mgr=None):
    """Run Orchestrator in standalone CLI mode."""
    # All CLI modes, including --dry-run and --one-gen, enter orchestrator
    # machinery and may create logs/checkpoints or invoke tools.  Refuse them
    # before configuring runtime state when the one-time reset is absent.
    from epoch_authority import require_policy_epoch_initialized

    require_policy_epoch_initialized("orchestrator_cli")
    from logging_config import configure_logging
    configure_logging()
    os.makedirs(LOGS_DIR, exist_ok=True)

    log_file = LOGS_DIR / f"orchestrator_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    mode = 'dry-run' if args.dry_run else 'one-gen' if args.one_gen else 'continuous'
    log.info("Starting. Mode: %s", mode)
    log.info("Log: %s", log_file)

    # In CLI mode, inject None (uses ToolUI fallback)
    inject_ui(None)
    set_system_log_ui(None)
    try:
        from llm_query import set_shutdown_manager
        set_shutdown_manager(shutdown_mgr)
    except Exception:
        pass

    try:
        operator_cost_policy = configure_runtime_cost_policy(
            load_operator_generation_cost_policy()
        )
    except CostPolicyConfigurationError as exc:
        log.error("Invalid operator generation cost policy: %s", exc)
        log_system_event(
            "orchestrator.cost_policy_invalid",
            "error",
            f"Invalid operator generation cost policy: {exc}",
            {"operator_action_required": True},
        )
        return 5

    # Standalone one-shot/dry-run modes do not enter ``orchestrator_loop``, so
    # prove recovery and consume the acknowledgement here.  Continuous mode
    # performs both operations inside the loop, eliminating a preflight-to-loop
    # race while preserving the same recovery-before-ack order.
    startup_recovery = _STARTUP_RECOVERY_UNSET
    if args.one_gen or args.dry_run:
        startup_recovery = _startup_recovery(None)
        startup_terminal_cost = _startup_recovery_terminal_cost(
            startup_recovery
        )
        if startup_terminal_cost is not None:
            return _one_generation_exit_code(startup_terminal_cost)
        try:
            consume_operator_resume_ack_from_env()
        except Exception as exc:
            log.exception("Invalid/unwritable LLM availability pause state: %s", exc)
            log_system_event(
                "orchestrator.llm_availability_state_invalid",
                "error",
                f"Invalid/unwritable LLM availability pause state: {exc}",
                {"operator_action_required": True},
            )
            return 5

    try:
        if args.one_gen or args.dry_run:
            if args.dry_run:
                cost = await _run_one_cycle(
                    ui=None,
                    log_file=log_file,
                    one_gen=args.one_gen,
                    dry_run=args.dry_run,
                    max_turns=args.max_turns,
                    _cost_policy=operator_cost_policy,
                )
            else:
                cost = await _run_one_generation_cli(
                    log_file=log_file,
                    max_turns=args.max_turns,
                    shutdown_mgr=shutdown_mgr,
                    cost_policy=operator_cost_policy,
                    startup_recovery=startup_recovery,
                )
            if args.one_gen:
                try:
                    cost_status = generation_cost_status()
                except Exception as exc:
                    log.exception(
                        "One-gen durable accounting status is unavailable: %s",
                        exc,
                    )
                    return 6
                if cost_status.get("active") is True:
                    log.info(
                        "One-gen durable spend: $%.6f (accounting_ok=%s, "
                        "generation_id=%s)",
                        float(cost_status.get("spent_usd") or 0.0),
                        cost_status.get("accounting_ok"),
                        cost_status.get("generation_id"),
                    )
            if cost == ORCH_GENERATION_ABANDONED_COST:
                log.warning(
                    "One-gen ended at a verified canonical abandon boundary; "
                    "no successor workflow was prepared."
                )
            elif cost == ORCH_OPERATOR_ACTION_REQUIRED_COST:
                log.warning(
                    "One-gen parked at an operator-only boundary; generation "
                    "publication is not complete."
                )
            elif cost == ORCH_RECOVERY_BLOCKED_COST:
                log.error(
                    "One-gen stopped fail-closed on recovery authority; "
                    "generation publication is not complete."
                )
            elif cost == ORCH_ACCOUNTING_BLOCKED_COST:
                log.error(
                    "One-gen publication completed, but durable generation-cost "
                    "accounting is invalid; operator reconciliation is required."
                )
            else:
                log.info("Done. Cost: $%.4f", cost)
            if args.one_gen:
                return _one_generation_exit_code(cost)
            return 0 if cost >= 0 else 5
        else:
            outcome = await orchestrator_loop(
                ui=None,
                shutdown_mgr=shutdown_mgr,
                no_daemon=args.no_daemon,
            )
            return _continuous_exit_code(outcome)
    finally:
        deactivate_generation_cost_scope()
        try:
            from evolution_infra import stop_daemon
            stop_daemon()
        except Exception:
            pass


def main():
    import signal
    parser = argparse.ArgumentParser(
        description="national_tcp_policy_v1 LLM evolution orchestrator"
    )
    parser.add_argument("--one-gen", action="store_true", help="Run one generation then stop")
    parser.add_argument("--dry-run", action="store_true", help="Only check status, no changes")
    parser.add_argument("--no-daemon", action="store_true", help="Skip daemon startup")
    parser.add_argument("--max-turns", type=int, default=None, help="Max tool call turns per cycle")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown_mgr = ShutdownManager(grace_period=15.0)
    shutdown_mgr.install_signal_handlers(loop)

    exit_code = 0
    try:
        exit_code = int(
            loop.run_until_complete(run_orchestrator_cli(args, shutdown_mgr))
            or 0
        )
    except KeyboardInterrupt:
        log.warning("Forced exit.")
        exit_code = 130
    finally:
        loop.close()
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
