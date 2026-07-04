"""Evolution Orchestrator — LLM-driven bot evolution pipeline.

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
import json
import os
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
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    CLINotFoundError,
    ProcessError,
    ClaudeSDKError,
)
from tools import evolution_server, inject_ui
from llm_failure import is_llm_infra_error, is_shutdown_cancel_error as _is_shutdown_cancel_error
from shutdown_manager import ShutdownManager
from system_log import log_system_event, set_ui as set_system_log_ui
from failure_classification import INFRA_BLOCKER_REASONS
import logging

log = logging.getLogger("pok.orchestrator")
os.environ.setdefault("POK_WORKFLOW_PROFILE", "national_native")
SHUTDOWN_CANCEL_COST = -99998.0

# Infra-only blocker reasons used by the timed_out handler to distinguish
# scheduler/daemon failures from real bot regressions.
_INFRA_BLOCKER_REASONS_SET = frozenset(INFRA_BLOCKER_REASONS)


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


class _CostCapTripped(Exception):
    """Internal signal: cycle spend exceeded MAX_GEN_COST. Hard-stop the LLM
    stream immediately instead of burning 26-32min until CYCLE_TIMEOUT.

    root-cause-audit 2026-06-21: _check_cost_cap() previously only logged the
    overrun and relied on CYCLE_TIMEOUT to bound the cycle — verified v139/v143
    ran 26-32min of pure waste after cap-tripped. Raising propagates out of the
    `async for message in gen` loop into _run_one_cycle's except handler, which
    classifies it as infra (−0.5 short backoff) and clears the session so resume
    can't keep burning budget on the same runaway cycle.
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


# Module-level flag set by the watchdog coroutine when it detects a stuck pipeline.
# The main orchestrator_loop checks this flag at the top of each iteration and forces
# a fresh _run_one_cycle (discarding the stale session) when set.
_watchdog_triggered = False
ORCH_FIRST_ACTIVITY_TIMEOUT = int(os.environ.get("POK_ORCH_FIRST_ACTIVITY_TIMEOUT", "600"))
ORCH_STREAM_POLL_INTERVAL = float(os.environ.get("POK_ORCH_STREAM_POLL_INTERVAL", "15"))
ORCH_ACTIONABLE_STAGE_TIMEOUT = float(os.environ.get("POK_ORCH_ACTIONABLE_STAGE_TIMEOUT", "300"))
POST_GENERATION_CLEANUP_TIMEOUT = int(os.environ.get("POK_POST_GENERATION_CLEANUP_TIMEOUT", "900"))

ORCHESTRATOR_PROMPT = (Path(__file__).parent / "prompts" / "orchestrator.md").read_text()
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

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

from orchestrator_context import _build_context, _make_precompact_hook, _make_bot_dir_guard_hook, set_cycle_start_time  # noqa: E402
from orchestrator_session import (  # noqa: E402
    _rotate_orchestrator_logs, _is_rate_limited,
    _save_orchestrator_session, _load_orchestrator_session, _clear_orchestrator_session,
    _startup_recovery,
)
from evolution_infra import find_current_v  # noqa: E402
from llm_query import extract_result_error  # noqa: E402


_ACTIONABLE_STALL_STAGES = frozenset({
    "master_planned",
    "quality_failed",
    "precommit_failed",
    "repair_planned",
    "rework_running",
})


def _detect_actionable_stage_stall(timeout_sec=None):
    """Return checkpoint route data when a deterministic next-tool stage is stale."""
    timeout = ORCH_ACTIONABLE_STAGE_TIMEOUT if timeout_sec is None else float(timeout_sec)
    if timeout <= 0 and timeout_sec is None:
        return None
    try:
        from evolution_core import read_pipeline_checkpoint
        from pipeline_state import route_policy
        checkpoint = read_pipeline_checkpoint()
    except Exception:
        return None
    if not checkpoint:
        return None
    stage = checkpoint.get("stage")
    if stage not in _ACTIONABLE_STALL_STAGES:
        return None
    last_ts = (
        float(checkpoint.get("last_stage_change_ts") or 0.0)
        or float(checkpoint.get("last_update_ts") or 0.0)
    )
    if last_ts <= 0:
        return None
    elapsed = time.time() - last_ts
    if elapsed < timeout:
        return None
    route = route_policy(checkpoint)
    return {
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": stage,
        "elapsed_sec": round(elapsed, 1),
        "timeout_sec": timeout,
        "next_tool": route.get("next_tool"),
        "directive": route.get("directive"),
    }


def _detect_actionable_stage_handoff():
    """Return route data when an MCP gate has just produced a deterministic step."""
    stall = _detect_actionable_stage_stall(timeout_sec=0)
    if not stall:
        return None
    if stall.get("next_tool") != "execute_workers":
        return None
    return stall


async def _await_next_stream_message(stream_iter):
    """Wait for the next orchestrator stream message with checkpoint-aware polling."""
    pending = asyncio.create_task(stream_iter.__anext__())
    try:
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=max(0.1, ORCH_STREAM_POLL_INTERVAL),
                )
            except asyncio.TimeoutError:
                stall = _detect_actionable_stage_stall()
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
                pending.cancel()
                try:
                    await pending
                except BaseException:
                    pass
                raise _OrchActionableStageTimeout(msg)
    except BaseException:
        if not pending.done():
            pending.cancel()
        raise

async def _run_one_cycle(ui, log_file, one_gen=False, dry_run=False, max_turns=None, gen_ctx=None, shutdown_mgr=None):
    """Run one Orchestrator cycle (one LLM agent session). Returns total cost."""
    set_cycle_start_time(time.time())
    context = _build_context(one_gen=one_gen, dry_run=dry_run, gen_ctx=gen_ctx)
    prompt = ORCHESTRATOR_PROMPT.replace("{context}", context)

    if dry_run:
        prompt += "\n\nIMPORTANT: This is a DRY RUN. Only call get_status() and report the current state. Do NOT modify anything."

    # Session resume: if orchestrator_session.json exists (written on every tool call),
    # the previous cycle was interrupted — resume the exact conversation.
    # The file is cleared on natural cycle completion, so its presence reliably means
    # the process was killed mid-gen.  No need to gate this on pipeline_state.json.
    from evolution_core import read_pipeline_checkpoint
    checkpoint = read_pipeline_checkpoint()
    saved_session_id = _load_orchestrator_session()

    resume_kwargs = {"resume": saved_session_id} if saved_session_id else {}
    if saved_session_id and ui:
        stage_info = checkpoint.get("stage", "unknown") if checkpoint else "no checkpoint"
        ui.log_history(
            f"[Orchestrator] Resuming session {saved_session_id[:8]}... "
            f"(pipeline stage={stage_info})",
            "warn",
        )

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
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        hooks=_hooks,
        max_turns=max_turns,
        thinking={"type": "adaptive"},  # let Claude decide thinking depth (was disabled to dodge an old SDK signature bug; adaptive is now the documented default)
        **resume_kwargs,
    )

    total_cost = 0.0
    cycle_completed = False
    auth_error = False
    cycle_failed = False  # P1: generic-exception path must not return partial cost (fake success)
    infra_error = False  # P2: SDK signature/timeout/connection — distinct from real auth (-0.5 vs -1.0)
    shutdown_cancelled = False
    # Snapshot sub-agent costs at start to compute delta on return.
    # ui.gen_cost_total tracks ALL sub-agent costs (Master, Workers, etc.)
    # via ui.update_cost() called from llm_query.py. The orchestrator's own
    # session cost (total_cost from ResultMessage) is added below.
    _cost_at_start = ui.gen_cost_total if ui else 0.0

    with open(log_file, "a") as lf:
        lf.write(f"\n{'='*60}\n[ORCHESTRATOR CYCLE] {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")
        lf.write(f"[PROMPT]\n{prompt}\n\n[OUTPUT]\n")

        async def _stream_response(opts, max_retries=3):
            """Run a single streaming query. Returns (full_text, cost, cycle_ok, gen, auth_error)."""
            texts = []
            cost = 0.0
            ok = False
            gen = None
            auth_err = False
            _tool_call_counts = {}
            _cost_cap_logged = False

            def _check_cost_cap():
                """Surface runaway sub-agent spend.

                Checked after every AssistantMessage turn and at ResultMessage — NOT
                only in ToolResultBlock (which the claude_agent_sdk never surfaces in
                the orchestrator's message stream: tool_result logs appear 0×, making
                the old single check-point dead code). root-cause-audit 2026-06-17
                found cost_cap_tripped fired 0× despite cycles hitting $6.09 against a
                $5.0 cap. Sub-agent (Master/Workers/Critic) costs settle into
                ui.gen_cost_total via ui.update_cost before the next AssistantMessage,
                so checking on each turn catches runaway retries as they happen.
                """
                nonlocal _cost_cap_logged
                if _cost_cap_logged or not ui:
                    return
                try:
                    from evolution_infra import MAX_GEN_COST
                    _spent = ui.gen_cost_total - _cost_at_start
                except Exception as _e:
                    log.debug("cost-cap check error: %s", _e)
                    return
                if _spent > MAX_GEN_COST:
                    _cost_cap_logged = True
                    log.warning("Cycle cost cap tripped: $%.2f > $%.2f", _spent, MAX_GEN_COST)
                    if ui:
                        ui.log_history(
                            f"[Orchestrator] Cost cap tripped (${_spent:.2f} > ${MAX_GEN_COST:.2f}) — "
                            f"hard-stopping stream (was: runaway retry burned 26-32min until CYCLE_TIMEOUT).",
                            "error",
                        )
                    try:
                        log_system_event("pipeline.cost_cap_tripped", "error",
                            f"Cycle spend ${_spent:.2f} exceeded cap ${MAX_GEN_COST}",
                            {"spent": round(_spent, 2), "cap": MAX_GEN_COST})
                    except Exception:
                        pass
                    # 硬熔断：raise 在 try/except 之外（不被上方 cost-cap-check 的 except 吞），
                    # 传播到 _run_one_cycle 的 except Exception 归为 infra(-0.5 短退避) + 清 session。
                    raise _CostCapTripped(f"spend ${_spent:.2f} > cap ${MAX_GEN_COST}")

            try:
                gen = claude_query(prompt=prompt, options=opts)
                _gen_ref[0] = gen  # Track for asyncio.wait_for timeout cleanup
                _stream_iter = gen.__aiter__()
                _first_activity_seen = False
                _stream_started_at = time.time()
                while True:
                    try:
                        if not _first_activity_seen:
                            message = await asyncio.wait_for(
                                _stream_iter.__anext__(),
                                timeout=ORCH_FIRST_ACTIVITY_TIMEOUT,
                            )
                            _first_activity_seen = True
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
                            message = await _await_next_stream_message(_stream_iter)
                    except StopAsyncIteration:
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
                        for block in message.content:
                            if isinstance(block, TextBlock):
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
                                if ui:
                                    ui.log_history(f"[Orchestrator] Calling tool: {block.name}", "info")
                                    ui.log_io(f"\n[tool: {block.name}]", "tool", "Orchestrator")
                                    ui.emit_tool_call(block.name, block.input, "Orchestrator")
                                else:
                                    log.info("Calling tool: %s", block.name)
                                args_str = json.dumps(block.input, ensure_ascii=False, indent=2)[:2000]
                                lf.write(f"\n[tool: {block.name}]\n[args] {args_str}\n")
                                tool_name = block.name.split('__')[-1] if '__' in block.name else block.name
                                _tool_call_counts[tool_name] = _tool_call_counts.get(tool_name, 0) + 1
                                _thresh = _REDUNDANT_NOISY_THRESHOLD if tool_name in _NOISY_TOOLS else _REDUNDANT_STRICT_THRESHOLD
                                if _tool_call_counts[tool_name] == _thresh:
                                    # Warn exactly once when the threshold is first hit (not on every
                                    # subsequent call). Noisy tools get a high threshold; pipeline
                                    # tools stay strict. See _NOISY_TOOLS docstring.
                                    log.warning("Tool '%s' called %d times (possible redundant call)", tool_name, _tool_call_counts[tool_name])
                                    try:
                                        log_system_event("pipeline.redundant_tool_call", "warn",
                                            f"Orchestrator called {tool_name} {_tool_call_counts[tool_name]}x in one cycle",
                                            {"tool": tool_name, "count": _tool_call_counts[tool_name],
                                             "threshold": _thresh})
                                    except Exception:
                                        pass
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
                        # Check cost cap after each orchestrator turn. By now every
                        # sub-agent (Master/Workers/Critic) cost from tools executed
                        # during this turn has settled in ui.gen_cost_total.
                        _check_cost_cap()
                        handoff = _detect_actionable_stage_handoff()
                        if handoff:
                            next_v = handoff.get("next_v")
                            stage = handoff.get("stage")
                            next_tool = handoff.get("next_tool") or "unknown"
                            msg = (
                                f"Checkpoint reached actionable stage '{stage}' for v{next_v}; "
                                f"handing off current Orchestrator stream so recovery can call {next_tool} deterministically."
                            )
                            try:
                                log_system_event(
                                    "pipeline.actionable_stage_handoff",
                                    "warn",
                                    msg,
                                    handoff,
                                )
                            except Exception:
                                pass
                            raise _OrchActionableStageTimeout(msg)
                    elif isinstance(message, ResultMessage):
                        if message.total_cost_usd:
                            cost += message.total_cost_usd
                        _check_cost_cap()
                        if not message.is_error:
                            ok = True
                            if message.session_id:
                                _save_orchestrator_session(message.session_id)
                        else:
                            error_text = extract_result_error(message)
                            lf.write(f"\n[API ERROR] {error_text}\n")
                            if ui:
                                ui.log_history(f"[Orchestrator] API error: {error_text[:200]}", "error")
                            # 429 quota exhaustion: parse reset time but PRESERVE session
                            # so _run_one_cycle can resume via saved_session_id after the wait.
                            is_429 = "429" in error_text or ("已达到" in error_text and "使用上限" in error_text)
                            if is_429:
                                from rate_limiter import rate_limiter
                                rate_limiter.parse_429(error_text)
                                # Do NOT clear session — preserve for resume after reset
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
            except (CLINotFoundError, ProcessError) as e:
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
                # Signature-field stream errors: transient SDK deserialization bug
                # (ThinkingBlock.signature missing). Retryable ONLY when no MCP tool
                # has executed yet — otherwise tool side-effects would be duplicated.
                # When retryable, convert to _OrchSignatureRetryable for the retry
                # loop in _run_one_cycle; otherwise propagate (-> infra -0.5 backoff).
                _sig_s = str(_sig_err).lower()
                if ("signature" in _sig_s or "missing required field" in _sig_s) and not _tool_call_counts:
                    try:
                        if gen is not None:
                            await gen.aclose()
                    except Exception:
                        pass
                    raise _OrchSignatureRetryable(str(_sig_err)) from _sig_err
                raise
            if _tool_call_counts:
                log.info("Tool call summary: %s", dict(sorted(_tool_call_counts.items())))
            return "".join(texts), cost, ok, gen, auth_err

        CYCLE_TIMEOUT = 5400  # 90 minutes max per cycle. 实测各阶段 elapsed_sec median: master 597s + workers 624s + quality 101 + review 140 + critic 236 + precommit 1368s = 3066s(51min); mean ≈56min. 加 direction_audit/prepare/commit/archivist + API 慢重试 buffer → 3600 频繁超时(82× in 19h). 5400 = mean(56min) + ~34min buffer 覆盖 max case(master 2449s/workers 2276s). (was 3600, before that 1800s)
        # Sentinel returned by the timeout-extension path (stage=verified, first extension).
        # Must be DISTINCT from every other cost signal: -0.5 (infra), -1.0 (generic crash),
        # and the auth clamp -max(abs(total_cost), 1.0) which can reach any negative value
        # ≥1.0 in magnitude. -99999.0 is unreachable in practice (a single cycle cannot
        # spend $99999) so it cannot collide with the auth clamp even if a future cycle's
        # total_cost grew large. This fixes the v101 death-loop's latent collision risk.
        _TIMEOUT_EXTENSION_SENTINEL = -99999.0
        query_gen = None
        # Mutable container to track the async generator across scope boundaries.
        # asyncio.wait_for raises TimeoutError BEFORE tuple unpacking completes,
        # so query_gen remains None. We store gen here from inside _stream_response.
        _gen_ref = [None]
        # H1: clear the precommit shutdown flag at the start of every cycle so a
        # previous cycle's CYCLE_TIMEOUT doesn't poison the next precommit round.
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
                            await asyncio.wait_for(_stream_response(options), timeout=CYCLE_TIMEOUT)
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
                # query_gen is always None here (tuple unpacking never completed).
                # Use _gen_ref which was set at the start of _stream_response.
                _timed_out_gen = _gen_ref[0] or query_gen
                if _timed_out_gen is not None:
                    try:
                        await _timed_out_gen.aclose()
                    except Exception as e:
                        log.debug("gen.aclose failed during timeout: %s", e)

                # H1+H2 (2026-06-29): signal in-flight precommit mirror battles to
                # abort. wait_for cancels the stream + aclose()s the generator, but
                # mirror battles run via loop.run_in_executor (ThreadPool) whose
                # Future cannot be cancelled once running — subprocesses keep
                # spawning for up to per_game_timeout. The thread-safe flag set here
                # is checked between games inside the drain loops (tool_eval.py), so
                # the stalled precommit breaks out instead of exhausting the daemon
                # worker pool for hours (root cause of the v214-from-v212 5h stall).
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
                            # The generator is dead (asyncio.wait_for killed it) — the session
                            # cannot be resumed.  Clear it so the next _run_one_cycle starts fresh
                            # but resumes from the preserved checkpoint stage.
                            _clear_orchestrator_session()
                            # Refresh checkpoint timestamp so the watchdog does not immediately
                            # re-trigger on the next cycle (elapsed > WATCHDOG_TIMEOUT), AND
                            # record the single granted extension (timeout_extensions=1).
                            try:
                                from evolution_core import write_pipeline_checkpoint
                                write_pipeline_checkpoint(
                                    _ckpt.get("next_v"),
                                    _ckpt.get("source_v"),
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
                            if ui and total_cost > 0:
                                ui.update_cost("Orchestrator", total_cost, None)
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
                    from evolution_core import read_pipeline_checkpoint, write_pipeline_checkpoint
                    ckpt = read_pipeline_checkpoint()
                    if ckpt and ckpt.get("stage") not in ("timed_out", "archived"):
                        # B3 (v125 retry-storm fix): if Master repeatedly failed this
                        # cycle (audit_attempt >= MAX_MASTER_TOTAL_FAILURES=4), the
                        # timeout was caused by the Master retry-storm itself — abandon
                        # now instead of marking timed_out + resuming into the same
                        # stuck Master loop. "verified" is excluded (it has its own
                        # extension path above; commit is the imminent idempotent step).
                        _b3_audit = int(ckpt.get("audit_attempt") or 0)
                        _b3_stage = ckpt.get("stage")
                        _B3_MASTER_FAIL_THRESHOLD = 4  # mirrors MAX_MASTER_TOTAL_FAILURES (tool_planning.py)
                        if (_b3_audit >= _B3_MASTER_FAIL_THRESHOLD
                                and _b3_stage not in ("verified", "archived")):
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
                                from tool_bot_management import _do_abandon_generation
                                await _do_abandon_generation(
                                    reason=f"cycle_timeout_master_stuck ({_b3_audit} fails)"
                                )
                            except Exception as _ae:
                                log.warning("B3 forced-abandon failed (%s) — falling back to timed_out", _ae)
                                write_pipeline_checkpoint(
                                    ckpt.get("next_v"), ckpt.get("source_v"), "timed_out",
                                    master_plan=ckpt.get("master_plan"),
                                )
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
                                log.warning(
                                    "Cycle timed out during precommit with no regression "
                                    "blocker (infra-only) — marking infra_timed_out so the "
                                    "next cycle retries precommit on the same code.",
                                )
                                log_system_event(
                                    "pipeline.cycle_timeout_infra", "warn",
                                    f"Cycle timed out after {CYCLE_TIMEOUT}s during precommit "
                                    f"(infra-only, no regression blocker) — preserving gate_results/code for retry",
                                    {"timeout_sec": CYCLE_TIMEOUT, "pipeline_stage": _b3_stage,
                                     "precommit_attempt": ckpt.get("precommit_attempt", 0)},
                                )
                                write_pipeline_checkpoint(
                                    ckpt.get("next_v"), ckpt.get("source_v"), "infra_timed_out",
                                    master_plan=ckpt.get("master_plan"),
                                )
                                if ui:
                                    ui.log_history(
                                        "[Orchestrator] Infra-only timeout during precommit — "
                                        "preserving code/gates; next cycle will retry precommit.",
                                        "warn",
                                    )
                            else:
                                # LOG GAP FIX (2026-06-30): plain timed_out (the most
                                # common timeout path) previously had NO structured
                                # event — only cycle_timeout_abandon/infra logged.
                                # Record stage + reason so timeouts are auditable.
                                try:
                                    log_system_event(
                                        "pipeline.cycle_timeout_plain", "error",
                                        f"Cycle timed out after {CYCLE_TIMEOUT}s at stage="
                                        f"{_b3_stage} — marking timed_out (next cycle restarts)",
                                        {"timeout_sec": CYCLE_TIMEOUT,
                                         "pipeline_stage": _b3_stage,
                                         "next_v": ckpt.get("next_v"),
                                         "source_v": ckpt.get("source_v"),
                                         "precommit_attempt": ckpt.get("precommit_attempt", 0),
                                         "master_fail_count": _b3_audit},
                                    )
                                except Exception:
                                    pass
                                write_pipeline_checkpoint(
                                    ckpt.get("next_v"), ckpt.get("source_v"), "timed_out",
                                    master_plan=ckpt.get("master_plan"),
                                )
                                if ui:
                                    ui.log_history(
                                        "[Orchestrator] Pipeline checkpoint marked as timed_out — next cycle will restart.",
                                        "warn",
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
                    # Add any partial Orchestrator session cost to UI tracking
                    if total_cost > 0:
                        ui.update_cost("Orchestrator", total_cost, None)
                    return ui.gen_cost_total - _cost_at_start
                return total_cost

            # 529 rate-limit retry with exponential backoff
            if _is_rate_limited(full_output):
                # Preserve the original session so retries can resume the same
                # conversation instead of starting from scratch.
                _saved_session_id = _load_orchestrator_session()
                _clear_orchestrator_session()
                _resume_kwargs = {"resume": _saved_session_id} if _saved_session_id else {}
                retry_opts = ClaudeAgentOptions(
                    model="sonnet",
                    permission_mode="bypassPermissions",
                    cwd=str(PROJECT_ROOT),
                    mcp_servers={"evolution": evolution_server},
                    strict_mcp_config=True,
                    disallowed_tools=_BLOCKED_MCP_TOOLS,
                    hooks={**_make_precompact_hook(), **_make_bot_dir_guard_hook()},
                    max_turns=max_turns,
                    thinking={"type": "adaptive"},  # let Claude decide thinking depth
                    **_resume_kwargs,
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
                    if query_gen is not None:
                        try:
                            await query_gen.aclose()
                        except Exception as e:
                            log.debug("gen.aclose failed during retry: %s", e)
                    try:
                        full_output, retry_cost, cycle_completed, query_gen, auth_error = (
                            await asyncio.wait_for(_stream_response(retry_opts), timeout=CYCLE_TIMEOUT)
                        )
                    except asyncio.TimeoutError:
                        _timed_out_gen = _gen_ref[0]
                        if _timed_out_gen is not None:
                            try:
                                await _timed_out_gen.aclose()
                            except Exception:
                                pass
                        raise  # Re-raise to outer timeout handler
                    total_cost += retry_cost
                    if not _is_rate_limited(full_output):
                        break
                else:
                    # All retries exhausted — original session is gone.
                    # Session was already cleared before retry loop.
                    log.warning("529 retries exhausted — original session %s lost",
                                _saved_session_id[:8] if _saved_session_id else "none")

            # 429 quota detected — exit cycle cleanly so orchestrator_loop can block
            from rate_limiter import rate_limiter
            if rate_limiter.is_blocked() and not cycle_completed:
                if ui:
                    ui.log_history(
                        "[Orchestrator] 429 配额耗尽。Session 保留，等待恢复后继续。",
                        "warn",
                    )
                if ui and total_cost > 0:
                    ui.update_cost("Orchestrator", total_cost, None)
                return (ui.gen_cost_total - _cost_at_start) if ui else total_cost

            if ui:
                ui.update_cost("Orchestrator", total_cost, None)
                total_cost = ui.gen_cost_total - _cost_at_start
            lf.write(f"\n[CYCLE DONE] cost=${total_cost:.4f}\n")

        except KeyboardInterrupt:
            if query_gen is not None:
                try:
                    await query_gen.aclose()
                except Exception as e:
                    log.debug("gen.aclose failed during interrupt: %s", e)
            if ui:
                ui.log_history("[Orchestrator] Interrupted by user.", "warn")
            else:
                log.warning("Interrupted by user.")
            lf.write("\n[INTERRUPTED]\n")

        except asyncio.CancelledError:
            if query_gen is not None:
                try:
                    await query_gen.aclose()
                except Exception as e:
                    log.debug("gen.aclose failed during cancel: %s", e)
            # H1: signal in-flight precommit battles to abort (cancel, like timeout,
            # strands executor subprocesses that can't be cancelled mid-run).
            try:
                from tool_eval import set_precommit_shutdown
                set_precommit_shutdown()
            except Exception:
                pass
            # Session file PRESERVED — next startup can resume from checkpoint
            if ui:
                ui.log_history("[Orchestrator] Cancelled — session preserved for resume.", "warn")
            else:
                log.warning("Cancelled — session preserved for resume.")
            lf.write("\n[CANCELLED — session preserved for resume]\n")
            raise

        except Exception as e:
            # aclose 真实 gen：query_gen 在元组解包成功时赋值，但异常路径（含 _CostCapTripped
            # 在 AssistantMessage 循环内 raise）可能在解包前抛出 → query_gen 为 None，真实 gen
            # 在 _gen_ref[0]。两者都尝试关（root-cause-audit bug-check：cost cap 路径泄漏 CLI subprocess）。
            for _g in (query_gen, _gen_ref[0]):
                if _g is not None:
                    try:
                        await _g.aclose()
                    except Exception as _ge:
                        log.debug("gen.aclose failed: %s", _ge)
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
                    isinstance(e, (_CostCapTripped, _OrchFirstActivityTimeout, _OrchActionableStageTimeout))
                    or _is_cycle_infra_error(e)
                )
            )
            # SDK streaming errors (missing 'signature' field on thinking blocks,
            # observed with claude_agent_sdk 0.2.91 + adaptive thinking) leave the
            # session broken — resuming replays into the same crash (the v84
            # quality_passed→run_review infinite-retry deadlock). P0 disabled
            # thinking so this no longer fires, but the guard prevents silent
            # recurrence if thinking is ever re-enabled or another SDK stream
            # error appears.
            if is_shutdown_cancel:
                shutdown_cancelled = True
                cycle_failed = False
                if ui:
                    ui.log_history(
                        "[Orchestrator] Claude stream stopped during shutdown; session preserved for resume.",
                        "warn",
                    )
                else:
                    log.warning("Claude stream stopped during shutdown; session preserved for resume: %s", e)
                try:
                    log_system_event(
                        "orchestrator.shutdown_cancelled",
                        "info",
                        "Claude stream stopped during orchestrator shutdown; session preserved",
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
                        "session cleared; next cycle will resume from checkpoint when present.", "warn",
                    )
                else:
                    log.warning(
                        "LLM infra error (%s), session cleared; checkpoint resume will be attempted: %s",
                        type(e).__name__, e,
                    )
                try:
                    log_system_event("pipeline.sdk_stream_error", "warn",
                        f"Orchestrator LLM infra error ({type(e).__name__}): {e}",
                        {"session_cleared": True, "exception_type": type(e).__name__})
                except Exception:
                    pass
            else:
                # Session file PRESERVED — next startup can assess recovery
                if ui:
                    ui.log_history(f"[Orchestrator] Error: {e}", "error")
                else:
                    log.error("Error: %s", e)
            lf.write(f"\n[{'SHUTDOWN_CANCELLED' if is_shutdown_cancel else 'ERROR'}] {e}\n")

    # Only clear session file on natural (non-error) cycle completion.
    # If killed, the session file remains so next startup can resume.
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
        if ui and total_cost > 0:
            ui.update_cost("Orchestrator", total_cost, None)
        return -0.5 if infra_error else -1.0

    # On non-happy paths (KeyboardInterrupt — explicit user interrupt), total_cost
    # may only be the Orchestrator's partial session cost. Return the full tracked
    # cost delta when UI is available.
    if ui and not cycle_completed:
        if total_cost > 0:
            ui.update_cost("Orchestrator", total_cost, None)
        return ui.gen_cost_total - _cost_at_start

    return total_cost


def _checkpoint_recovery_context(reason: str, ui=None):
    """Build a recovery context from an active pipeline checkpoint.

    LLM sessions are disposable after SDK/cost-cap failures; pipeline checkpoints
    are not. This helper keeps those concepts separate so an infra retry resumes
    the same generation instead of falling back to Phase 1 source selection.
    """
    try:
        from evolution_core import read_pipeline_checkpoint
        checkpoint = read_pipeline_checkpoint()
    except Exception as e:
        log.debug("checkpoint recovery read failed (%s): %s", reason, e)
        return None

    if not checkpoint:
        return None

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

    dead_stages = {None, "timed_out", "infra_timed_out", "archived", "abandoned"}
    if stage in dead_stages:
        return None

    recovery = {
        "action": "resume",
        "checkpoint": checkpoint,
        "session_id": None,  # force a fresh LLM session, but keep pipeline identity
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
    }

    msg = f"[Recovery] Resuming v{next_v} at '{stage}' after {reason} (new LLM session)."
    if ui:
        ui.log_history(msg, "warn")
    else:
        log.warning(msg)
    try:
        log_system_event(
            "orchestrator.recovery_decision", "warn", msg,
            {"case": f"resume_after_{reason}",
             "next_v": next_v, "source_v": source_v,
             "stage": stage, "session_present": False},
        )
    except Exception:
        pass
    return recovery


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
    return (
        "CIRCUIT BREAKER" in error
        or error == "WORKER_CIRCUIT_BREAKER_CROSS_GEN"
    )


def _is_precommit_rework_circuit_breaker_result(data):
    if not isinstance(data, dict):
        return False
    return str(data.get("error") or "") == "PRECOMMIT_REWORK_CIRCUIT_BREAKER"


async def _try_deterministic_checkpoint_route(recovery, ui=None):
    """Execute safe checkpoint routes without asking the Orchestrator LLM again."""
    if not recovery or recovery.get("action") != "resume":
        return False
    checkpoint = recovery.get("checkpoint") or {}
    stage = checkpoint.get("stage")
    if stage not in _ACTIONABLE_STALL_STAGES:
        return False
    saved_session_id = _load_orchestrator_session()
    if saved_session_id and stage == "master_planned":
        _clear_orchestrator_session(reason="deterministic_master_planned_route")
    elif saved_session_id:
        return False

    try:
        from pipeline_state import route_policy
        route = route_policy(checkpoint)
    except Exception:
        route = {}
    if route.get("next_tool") != "execute_workers":
        return False

    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    if next_v is None or source_v is None:
        return False

    msg = (
        f"[Recovery] Deterministically routing v{next_v} at {stage} "
        "to execute_workers with checkpoint gate feedback."
    )
    if ui:
        ui.log_history(msg, "warn")
    else:
        log.warning(msg)
    try:
        log_system_event(
            "pipeline.deterministic_route_execute_workers",
            "warn",
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

    from tool_planning import execute_workers
    result = await execute_workers.handler({"next_v": next_v, "source_v": source_v})
    data = _extract_tool_result_json(result)
    error = data.get("error")
    success = data.get("success")
    if error:
        if _is_worker_circuit_breaker_result(data) or _is_precommit_rework_circuit_breaker_result(data):
            from tool_bot_management import _do_abandon_generation

            abandon_reason = (
                "precommit_rework_circuit_breaker"
                if _is_precommit_rework_circuit_breaker_result(data)
                else "worker_circuit_breaker"
            )
            abandon_result = await _do_abandon_generation(reason=abandon_reason)
            abandoned = bool(abandon_result.get("abandoned"))
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

        detail = f"Deterministic execute_workers route failed for v{next_v}: {str(error)[:180]}"
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
        log_system_event(
            "pipeline.deterministic_route_done",
            "success" if success else "warn",
            f"Deterministic execute_workers route completed for v{next_v}",
            {"next_v": next_v, "source_v": source_v, "stage": stage, "success": success},
        )
    except Exception:
        pass
    return True


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
            f"{POST_GENERATION_CLEANUP_TIMEOUT}s; continuing evolution."
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
      - A session file exists (orchestrator is actively running a cycle)
      - The checkpoint stage is in the recoverable set
      - No stage change for > WATCHDOG_TIMEOUT seconds
    """
    global _watchdog_triggered
    from evolution_infra import WATCHDOG_TIMEOUT
    from evolution_core import read_pipeline_checkpoint

    recoverable_stages = {"selected", "preparing", "prepared", "crossover_running",
                          "direction_audited", "master_planned", "workers_done",
                          "quality_failed", "quality_passed", "reviewed",
                          "critic_checked", "precommit_failed", "repair_planned",
                          "rework_running", "verified"}

    while True:
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return
        try:
            await asyncio.sleep(check_interval)
            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                return

            # Only trigger if orchestrator session exists (cycle is active)
            session_id = _load_orchestrator_session()
            if not session_id:
                continue

            checkpoint = read_pipeline_checkpoint()
            if not checkpoint:
                continue

            stage = checkpoint.get("stage", "unknown")
            if stage not in recoverable_stages:
                continue

            last_ts = checkpoint.get("last_stage_change_ts", 0.0)
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


async def orchestrator_loop(ui, shutdown_mgr=None, no_daemon=False, daemon_workers=None, daemon_pairs=5):
    """Orchestrator entry point — three-phase generation loop.

    Args:
        ui: BaseUI instance (WebUI for Dashboard). Can be None for silent mode.
        shutdown_mgr: ShutdownManager for graceful signal handling.
        no_daemon: If True, skip daemon startup.
        daemon_workers: Number of parallel workers for the daemon subprocess.
        daemon_pairs: Mirror pairs per match for the daemon subprocess.
    """
    if daemon_workers is None:
        daemon_workers = max(1, int(os.cpu_count() * 28 / 32))
    from tools import inject_ui
    inject_ui(ui)
    set_system_log_ui(ui)
    try:
        from llm_query import set_shutdown_manager
        set_shutdown_manager(shutdown_mgr)
    except Exception:
        pass

    os.makedirs(LOGS_DIR, exist_ok=True)
    _rotate_orchestrator_logs(LOGS_DIR)

    if ui:
        ui.log_history("🔥 Orchestrator starting...", "success")
        ui.set_header("🔥 LLM Orchestrator Evolution 🔥")

    log_system_event("orchestrator.started", "success", "Orchestrator started",
                     {"daemon_enabled": not no_daemon})
    log.info("Orchestrator loop started (daemon=%s)", not no_daemon)

    # Start daemon
    _daemon_stop = None
    if not no_daemon:
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

    # Startup recovery — assess interrupted state
    recovery = _startup_recovery(ui)

    # Launch background watchdog coroutine to detect stuck pipelines
    _watchdog_task = asyncio.create_task(
        _watchdog_coroutine(ui, shutdown_mgr, check_interval=60)
    )

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

            # 429 quota exhaustion check — block until reset, then resume
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
                # Do NOT clear session — next _run_one_cycle() will resume via saved session
                continue

            if recovery is None:
                recovery = _checkpoint_recovery_context("active_checkpoint", ui)

            gen_count += 1
            log_system_event("orchestrator.cycle_start", "info", f"Cycle {gen_count} starting",
                             {"gen_count": gen_count})

            if recovery and recovery.get("action") == "blocked":
                diag = recovery.get("diagnostics") or {}
                issues = diag.get("issues") or []
                msg = (
                    "Startup recovery is blocked by an unrecoverable pipeline "
                    f"checkpoint: {', '.join(map(str, issues)) or recovery.get('reason')}"
                )
                if ui:
                    ui.log_history(f"[Orchestrator] {msg}", "error")
                    ui.set_status("Recovery blocked; manual checkpoint cleanup required", is_working=False)
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
                if await _try_deterministic_checkpoint_route(recovery, ui):
                    recovery = _checkpoint_recovery_context("deterministic_route", ui)
                    if ui:
                        ui.reset_gen_cost()
                    await asyncio.sleep(1)
                    continue
                from generation_scheduler import GenerationContext
                ckpt = recovery["checkpoint"]
                parent2_v = ckpt.get("parent2_v")
                strategy = "crossover" if parent2_v else "master"
                gen_ctx = GenerationContext(
                    current_v=ckpt.get("source_v", find_current_v()),
                    next_v=ckpt["next_v"],
                    strategy=strategy,
                    source_v=ckpt["source_v"],
                    crossover_parents=(ckpt["source_v"], parent2_v) if parent2_v else (),
                    gen_count=gen_count,
                )
                recovery = None  # consume recovery, only used once
            else:
                # Phase 1: Prepare (disposable on interrupt)
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

            # Phase 2: Run one generation (preserves state on interrupt)
            cost = await _run_one_cycle(
                ui=ui,
                log_file=log_file,
                one_gen=False,
                dry_run=False,
                max_turns=None,
                gen_ctx=gen_ctx,
                shutdown_mgr=shutdown_mgr,
            )

            # Timeout-extension sentinel: a cycle timed out but commit was imminent
            # (stage=verified) so ONE extension was granted mid-cycle. The cycle is NOT
            # complete — the bot has not committed yet. Do NOT run post_generation_cleanup,
            # do NOT log 'gen complete', do NOT back off. Just resume from the checkpoint
            # next iteration. Must come BEFORE the cost >= 0 success block so the sentinel
            # is never treated as success. Value -99999.0 (distinct from auth clamp).
            if cost == -99999.0:
                if ui:
                    ui.log_history(
                        "Orchestrator: cycle timed out but commit was imminent — granted extension, "
                        "resuming from checkpoint next cycle (no commit yet).",
                        "warn",
                    )
                # Reset per-generation cost tracker for the continued cycle
                if ui:
                    ui.reset_gen_cost()
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
                    if ui:
                        ui.reset_gen_cost()
                    await asyncio.sleep(5)
                    continue
                # Reset the generic-failure backoff counter — the cycle succeeded.
                if getattr(orchestrator_loop, "_gen_fail_count", 0):
                    orchestrator_loop._gen_fail_count = 0
                await _run_post_generation_cleanup_with_timeout(
                    shutdown_mgr, ui, gen_ctx, gen_count=gen_count
                )
                if ui:
                    ui.log_history(f"Orchestrator gen {gen_count} complete. Cost: ${cost:.4f}", "info")
                log_system_event("orchestrator.cycle_done", "info", f"Cycle {gen_count} done (cost=${cost:.4f})",
                                 {"gen_count": gen_count, "cost": round(cost, 4)})
                # Reset per-generation cost tracker for next cycle
                if ui:
                    ui.reset_gen_cost()

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
    finally:
        if not _watchdog_task.done():
            _watchdog_task.cancel()
            try:
                await _watchdog_task
            except asyncio.CancelledError:
                pass
        if _daemon_stop is not None:
            _daemon_stop.set()
        # Don't stop daemon — it runs independently and survives orchestrator restarts
        # Daemon is only stopped on full process exit (app.py lifespan) or explicit stop


async def _prepare_or_fail(shutdown_mgr, ui, min_games=None):
    """Run prepare_generation with error handling. Returns ctx or None."""
    from generation_scheduler import prepare_generation
    try:
        return await prepare_generation(shutdown_mgr, ui, min_games=min_games)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if ui:
            ui.log_history(f"prepare_generation failed: {e}", "error")
        else:
            log.error("prepare_generation failed: %s", e)
        return None


async def run_orchestrator_cli(args, shutdown_mgr=None):
    """Run Orchestrator in standalone CLI mode."""
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
        if args.one_gen or args.dry_run:
            if args.dry_run:
                cost = await _run_one_cycle(
                    ui=None,
                    log_file=log_file,
                    one_gen=args.one_gen,
                    dry_run=args.dry_run,
                    max_turns=args.max_turns,
                )
            else:
                # one-gen mode: use three phases
                from generation_scheduler import prepare_generation
                gen_ctx = await prepare_generation(shutdown_mgr, None)
                if gen_ctx is None:
                    if shutdown_mgr and shutdown_mgr.is_shutting_down:
                        log.warning("Cancelled during preparation.")
                    else:
                        log.warning("Preparation returned no context.")
                    return
                cost = await _run_one_cycle(
                    ui=None, log_file=log_file,
                    one_gen=True, dry_run=False,
                    max_turns=args.max_turns,
                    gen_ctx=gen_ctx,
                )
                if cost >= 0:
                    await _run_post_generation_cleanup_with_timeout(
                        shutdown_mgr, None, gen_ctx, gen_count=1
                    )
            log.info("Done. Cost: $%.4f", cost)
        else:
            await orchestrator_loop(
                ui=None,
                shutdown_mgr=shutdown_mgr,
                no_daemon=args.no_daemon,
            )
    finally:
        try:
            from evolution_infra import stop_daemon
            stop_daemon()
        except Exception:
            pass


def main():
    import signal
    parser = argparse.ArgumentParser(description="LLM Evolution Orchestrator")
    parser.add_argument("--one-gen", action="store_true", help="Run one generation then stop")
    parser.add_argument("--dry-run", action="store_true", help="Only check status, no changes")
    parser.add_argument("--no-daemon", action="store_true", help="Skip daemon startup")
    parser.add_argument("--max-turns", type=int, default=None, help="Max tool call turns per cycle")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown_mgr = ShutdownManager(grace_period=15.0)
    shutdown_mgr.install_signal_handlers(loop)

    try:
        loop.run_until_complete(run_orchestrator_cli(args, shutdown_mgr))
    except KeyboardInterrupt:
        log.warning("Forced exit.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
