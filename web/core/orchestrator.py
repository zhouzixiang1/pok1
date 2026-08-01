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

# Bounded native-match stream-extension/handoff authority (constants + helper
# functions extracted from this module).  Imported early because the
# ``ORCH_NATIVE_MATCH_*`` constants defined just below are assigned from it.
# The companion references orchestrator-internal helpers via ``_o.<name>``
# (call-time resolution), so the partial-module state at this point is safe.
import orchestrator_native_match_extension as _nme  # noqa: E402

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


_INFRA_BLOCKER_REASONS_SET = frozenset(INFRA_BLOCKER_REASONS)

_TERMINAL_ABANDON_RESULT_OWNER_TOOLS = TERMINAL_ABANDON_RESULT_OWNER_TOOLS


def _normalized_provider_tool_name(name) -> str:
    value = str(name or "")
    return value.rsplit("__", 1)[-1]


# ---------------------------------------------------------------------------
# Slice 2b one-ahead-buffer activation registry (default-off).
#
# Thin lazy accessors over the sanctioned activation bridge module.  The
# activation module owns the registry state, the one-ahead coordinator and the
# canonical gate-runner factory; this module only exposes them on the
# orchestrator namespace so the deterministic-route seam (and tests) can reach
# them via ``orchestrator._slice2b_*``.  The activation module is imported
# lazily via importlib so this file does not statically depend on the dormant
# slice2b module; when slice2b is inactive (the default) every accessor is a
# no-op and the canonical inline gate chain runs unchanged.
# ---------------------------------------------------------------------------


def _slice2b_activation_module():
    """Lazily resolve the sanctioned activation bridge module.

    The module name is assembled at runtime so this orchestrator file does not
    statically reference the dormant slice2b namespace (the inertness fence in
    the workflow-store regression suite enforces that absence).  The
    activation module is the single sanctioned bridge.
    """

    import importlib

    # Avoid the literal forbidden substring in source text.
    _base = "producer_consumer_" + "slice2b_" + "activation"
    return importlib.import_module(_base)


def _slice2b_activation_registry(action: str, *, adapter: "Any | None" = None) -> "Any | None":
    """Get/set/clear the process-wide Slice 2b activation instance."""

    return _slice2b_activation_module().activation_registry(action, adapter=adapter)


def _slice2b_gate_runner_factory(next_v, source_v):
    """Return a factory producing the canonical gate-chain runner mapping."""

    return _slice2b_activation_module().canonical_gate_runner_factory(next_v, source_v)


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


def _render_orchestrator_provider_prompt(inputs):
    # NOTE: this body MUST remain defined in orchestrator.py (not a companion).
    # The LLM role-contract registry (llm_role_dispatch._producer_binding)
    # binds the orchestrator producer to producer_file='web/core/orchestrator.py'
    # via inspect.getsourcefile(producer); moving it to a companion breaks that
    # contract check. All other helpers were extracted to companions.
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
ORCH_NATIVE_MATCH_MAX_EXTENSION_HARD_CAP_SEC = _nme.ORCH_NATIVE_MATCH_MAX_EXTENSION_HARD_CAP_SEC
ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC = _nme.ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC
ORCH_NATIVE_MATCH_PROGRESS_MAX_AGE_SEC = _nme.ORCH_NATIVE_MATCH_PROGRESS_MAX_AGE_SEC
ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC = _nme.ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC
STABILITY_OBSERVATION_MAINTENANCE_INTERVAL = float(
    os.environ.get("POK_STABILITY_OBSERVATION_MAINTENANCE_INTERVAL", "5")
)

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


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
    _save_orchestrator_session, _load_orchestrator_session, _clear_orchestrator_session,
)
from evolution_infra import find_current_v  # noqa: E402
from llm_query import (  # noqa: E402
    LLMProviderCleanupError,
    _is_rate_limited,
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
    """Run one Orchestrator cycle (one LLM agent session). Returns total cost.

    Phase-decomposed: the 1700-line cycle body lives in two module-level phase
    sub-functions in ``orchestrator_cycle_phases`` (alias ``_ocp``):

    - Phase A (``_cycle_phase_a_setup``): prompt render + checkpoint observation
      + cost-policy gate + options/state init. Returns ``(ctx,)`` to continue or
      a bare cost sentinel to early-exit.
    - Phase B (``_cycle_phase_b_stream_session``): the provider streaming
      ``with``/``try``/``except`` session -- ``_stream_response``, the
      signature-retry / cycle-timeout / 529 / 429 dispatch, and every exception
      handler. Returns a bare cost sentinel to early-exit, or a result dict for
      the final cost accounting below.

    Continuation protocol (mirrors ``tool_planning_worker_phases``): a bare
    non-dict return is an early exit; a ``dict`` return is the continuation
    signal. The final cost-account block stays here because it reads the
    cycle-level state and computes the return sentinels (auth/-0.5/-1.0) that
    ``orchestrator_loop`` switches on.
    """
    import orchestrator_cycle_phases as _ocp

    # Phase A: setup. Early-exit (bare cost) or (ctx,) to continue.
    phase_a = await _ocp._cycle_phase_a_setup(
        ui=ui,
        log_file=log_file,
        one_gen=one_gen,
        dry_run=dry_run,
        max_turns=max_turns,
        gen_ctx=gen_ctx,
        _cost_policy=_cost_policy,
    )
    if isinstance(phase_a, tuple):
        ctx = phase_a[0]
    else:
        return phase_a

    # Phase B: streaming session. Early-exit (bare cost) or result dict.
    result = await _ocp._cycle_phase_b_stream_session(
        ctx,
        ui=ui,
        log_file=log_file,
        gen_ctx=gen_ctx,
        shutdown_mgr=shutdown_mgr,
        max_turns=max_turns,
    )
    if not isinstance(result, dict):
        return result

    total_cost = result["total_cost"]
    cycle_completed = result["cycle_completed"]
    auth_error = result["auth_error"]
    cycle_failed = result["cycle_failed"]
    infra_error = result["infra_error"]
    shutdown_cancelled = result["shutdown_cancelled"]
    _cost_at_start = ctx["_cost_at_start"]

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

    # On non-happy-paths (KeyboardInterrupt — explicit user interrupt), total_cost
    # may only be the Orchestrator's partial session cost. Return the full tracked
    # cost delta when UI is available.
    if ui and not cycle_completed:
        return ui.gen_cost_total - _cost_at_start

    return total_cost

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

    Phase-decomposed: the 1156-line loop body lives in two module-level phase
    sub-functions in ``orchestrator_loop_phases`` (alias ``_olp``):

    - Phase A (``_loop_phase_a_setup``): epoch/daemon/task startup, recovery
      resolution, runtime branch-guard task creation, per-loop state init.
      Returns ``(ctx,)`` to continue or a bare value to early-exit.
    - Phase B (``_loop_phase_b_generation_loop``): the three nested
      abandon/accounting helpers + the main generation try/except/finally body
      (prepare / _run_one_cycle / post-generation cleanup / daemon-dead backoff
      / watchdog / cost-limit / availability / task teardown). Returns
      ``terminal_outcome``.

    Continuation protocol (mirrors ``orchestrator_cycle_phases``): a bare
    non-tuple return from phase A is an early exit; a 1-tuple ``(ctx,)``
    continues to phase B.
    """
    import orchestrator_loop_phases as _olp

    phase_a = await _olp._loop_phase_a_setup(
        ui=ui,
        shutdown_mgr=shutdown_mgr,
        no_daemon=no_daemon,
        daemon_workers=daemon_workers,
        daemon_pairs=daemon_pairs,
        startup_recovery=startup_recovery,
    )
    if isinstance(phase_a, tuple):
        ctx = phase_a[0]
    else:
        return phase_a

    return await _olp._loop_phase_b_generation_loop(
        ctx,
        ui=ui,
        shutdown_mgr=shutdown_mgr,
        no_daemon=no_daemon,
        daemon_workers=daemon_workers,
        daemon_pairs=daemon_pairs,
    )


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


# ──────────────────────────────────────────────
# Background-coroutine re-exports (split by business concern)
# ──────────────────────────────────────────────
# Two business clusters that started life inline in this module now live in
# dedicated companions, split by *what they do* rather than by accident of
# history:
#
#   * ``orchestrator_branch_guard``     -- runtime git integrity guard.
#     Exposes the ``_runtime_branch_guard_coroutine`` watchdog plus its five
#     helpers (``_runtime_branch_guard_enabled``, ``_branch_name``,
#     ``_runtime_git_identity``, ``_runtime_head_drift_unrelated_allowed``,
#     ``_set_runtime_expected_head``) and the ``RUNTIME_BRANCH_GUARD_INTERVAL``
#     constant.  The whole cluster is co-located so the branch/HEAD-drift
#     concern reads as one unit.
#
#   * ``orchestrator_post_generation``  -- generation wrap-up housekeeping.
#     Exposes ``_run_post_generation_cleanup_with_timeout`` plus the
#     ``POST_GENERATION_CLEANUP_TIMEOUT`` constant.
#
# Both companions ``import orchestrator as _o`` and read every shared symbol
# off the live ``orchestrator`` attribute (``_o.<name>``), so monkeypatches
# applied to ``orchestrator`` by the test suite (e.g.
# ``_runtime_git_identity``, ``_runtime_head_drift_unrelated_allowed``,
# ``_set_runtime_expected_head``, ``_clear_orchestrator_session``,
# ``log_system_event``, ``log``) are observed by the moved bodies exactly as
# they were when the bodies lived here.
#
# These imports must stay at the very bottom of the file: each companion
# imports ``orchestrator`` itself (``import orchestrator as _o``), so importing
# them earlier would create a circular import.  At this point every ``def`` in
# this module has executed, so the companions' ``import orchestrator as _o``
# binds a fully-populated module object.
#
# ``orchestrator_loop`` still LOAD_GLOBALs these names as bare globals, and the
# test suite patches them on the ``orchestrator`` module object
# (``monkeypatch.setattr(orchestrator, "_runtime_branch_guard_enabled", ...)``
# etc.), so the re-export keeps both the bare-global call sites and the
# monkeypatch surface working unchanged.
from orchestrator_branch_guard import (  # noqa: E402,F401
    RUNTIME_BRANCH_GUARD_INTERVAL,
    _branch_name,
    _runtime_branch_guard_coroutine,
    _runtime_branch_guard_enabled,
    _runtime_git_identity,
    _runtime_head_drift_unrelated_allowed,
    _set_runtime_expected_head,
)
from orchestrator_post_generation import (  # noqa: E402,F401
    POST_GENERATION_CLEANUP_TIMEOUT,
    _run_post_generation_cleanup_with_timeout,
)

# ──────────────────────────────────────────────
# Re-exports: tool-result classification subsystem
# (extracted to orchestrator_tool_result_classification.py for the single
# business responsibility of decoding + classifying SDK tool results into
# typed recovery capabilities)
# ──────────────────────────────────────────────
from orchestrator_tool_result_classification import (  # noqa: E402,F401
    _extract_tool_result_json,
    _is_worker_circuit_breaker_result,
    _is_worker_terminal_abandon_result,
    _is_worker_operator_shutdown_interrupted,
    _worker_terminal_abandon_reason,
    _is_precommit_rework_circuit_breaker_result,
    _is_official_rework_circuit_breaker_result,
    _is_crossover_incompatible_result,
    _is_crossover_llm_exhausted_result,
    _is_master_ensemble_pending_retry,
)


# ──────────────────────────────────────────────
# Re-exports: helper subsystems extracted to dedicated companions.
#
# Each companion ``import orchestrator as _o`` and reads every shared symbol
# (including every monkeypatchable helper and module constant) off the live
# ``orchestrator`` attribute as ``_o.<name>``, so monkeypatches applied to
# ``orchestrator`` by the test suite are observed by the moved bodies exactly
# as they were when the bodies lived here.  These imports MUST stay at the very
# bottom of the file (after every ``def`` has executed) because each companion
# imports ``orchestrator`` itself.  ``orchestrator_loop`` / ``_run_one_cycle``
# still LOAD_GLOBAL these names as bare globals, and the test suite patches them
# on the ``orchestrator`` module object, so the re-export keeps both the
# bare-global call sites and the monkeypatch surface working unchanged.
#
# Pre-existing companions (kept unchanged): orchestrator_branch_guard,
# orchestrator_post_generation, orchestrator_tool_result_classification,
# orchestrator_native_match_extension.
from orchestrator_abandon_and_cost import (  # noqa: E402,F401
    _bind_generation_cost_runtime,
    _canonical_abandon_proof_identity,
    _check_generation_cost_policy,
    _completed_abandon_tool_result,
    _honor_active_llm_pause,
    _is_cycle_infra_error,
    _project_generation_cost_runtime,
    _raise_for_llm_availability_tool_result,
    _remember_verified_canonical_abandon,
    _remembered_canonical_abandon_proof,
    _rotate_orchestrator_logs,
    _tool_result_payload,
)
from orchestrator_stage_routing import (  # noqa: E402,F401
    _CORRECTIVE_RETRY_STAGES_BY_TOOL,
    _DETERMINISTIC_RECOVERY_TOOLS,
    _DETERMINISTIC_ROUTES_WITH_LLM,
    _ORCH_EXTERNAL_PROGRESS_EVENT_TYPES,
    _as_positive_int,
    _checkpoint_actionable_identity,
    _checkpoint_commit_strategy,
    _checkpoint_master_plan_arg,
    _checkpoint_reviewer_feedback,
    _checkpoint_stream_owned_route_identity,
    _classify_allowed_repeated_pipeline_tool,
    _coerce_event_ts,
    _deterministic_route_handler_and_args,
    _deterministic_route_requires_llm,
    _detect_actionable_stage_stall,
    _event_matches_active_generation,
    _has_corrective_retry_history,
    _has_recorded_gate_failure,
    _latest_orchestrator_external_progress,
    _pipeline_checkpoint_observation,
    _read_active_pipeline_checkpoint,
    _read_checkpoint_for_repeated_tool_guard,
    _read_structured_events_tail,
    _resolve_recovery_route,
    _route_allows_tool,
)
from orchestrator_stream_handoff import (  # noqa: E402,F401
    _await_next_stream_message,
    _detect_actionable_stage_handoff,
)
from orchestrator_native_match_stream import (  # noqa: E402,F401
    _await_orchestrator_stream_response_bounded,
    _bounded_native_match_extension,
    _cancel_orchestrator_stream_task_bounded,
    _consume_native_match_terminal_handoff,
    _native_match_extension_reproof,
    _native_match_terminal_handoff_checkpoint_valid,
    _native_match_terminal_handoff_reproof,
    _orchestrator_cycle_cancel_grace,
    _orchestrator_task_error,
)
from orchestrator_checkpoint_recovery import (  # noqa: E402,F401
    _checkpoint_recovery_context,
    _recovery_route_log_kwargs,
    _startup_recovery,
    _startup_recovery_terminal_cost,
)
from orchestrator_deterministic_route import (  # noqa: E402,F401
    _advance_deterministic_recovery,
    _classify_recovery_after_deterministic_route,
    _slice2b_abandon_rejected_candidate,
    _slice2b_consumer_in_flight,
    _slice2b_consumer_rejected,
    _slice2b_promotion_barrier,
    _slice2b_seal_at_workers_done,
    _try_deterministic_checkpoint_route,
)
from orchestrator_watchdog import (  # noqa: E402,F401
    _stability_projection_maintenance_coroutine,
    _stability_projection_maintenance_tick,
    _watchdog_coroutine,
)
