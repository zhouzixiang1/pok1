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
    _try_deterministic_checkpoint_route,
)
from orchestrator_watchdog import (  # noqa: E402,F401
    _stability_projection_maintenance_coroutine,
    _stability_projection_maintenance_tick,
    _watchdog_coroutine,
)
